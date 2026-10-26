"""One light entity per area."""

from collections import Counter
from collections.abc import Container, Mapping
import logging
from typing import Any, override

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_FLASH,
    ATTR_HS_COLOR,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    COLOR_MODES_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    brightness_supported,
    color_supported,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Context,
    Event,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.util.color import value_to_brightness

from .automation import RoomAutomation, manual_attributes
from .const import (
    BRIGHTNESS_THRESHOLD,
    COMMAND_INTERVAL_SECONDS,
    MANUAL_BRIGHTNESS_THRESHOLD,
    MANUAL_COLOR_TEMP_THRESHOLD_KELVIN,
    PAUSE_BRIGHTNESS,
    PAUSE_COLOR_TEMP,
    TRANSITION_GRACE_SECONDS,
)
from .manager import LightBuddyConfigEntry
from .offset import ColorTempOffset, same_color_temp
from .room import (
    COLOR_GROUP_ATTRS,
    MemberParams,
    RoomCommander,
    reachable_kelvin,
    turn_off_params,
)
from .schedule import Target

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# Some lights report on for a moment after a turn off, longer with transition.
TURNING_OFF_GRACE = 3
# A light reporting on this soon after it went off is not treated as turned on.
LIGHT_OFF_GRACE = 5

FORWARDED_FEATURES = (
    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one room light per area with lights.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.LIGHT] = async_add_entities


class RoomLight(LightEntity):
    """A light controlling all lights of an area."""

    PLATFORM = Platform.LIGHT

    _attr_available = False
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "room"

    def __init__(
        self,
        area_id: str,
        name: str,
        members: list[str],
        offsets: Mapping[str, ColorTempOffset],
        automation: RoomAutomation | None = None,
        separate: Container[str] = frozenset(),
    ) -> None:
        """Initialize the room light.

        Lights in separate get brightness and color as separate commands.
        """
        self.area_id = area_id
        self.area_name = name
        self.entity_id = f"{LIGHT_DOMAIN}.all_{area_id}"
        self._attr_unique_id = area_id
        self._attr_translation_placeholders = {"area": name}
        self._members = members
        self._offsets = offsets
        self._separate = separate
        self._automation = automation
        # Lights turning on before this loop time may still be turning off.
        self._turning_off_until = 0.0
        self._turned_off_at: dict[str, float] = {}
        # Lights turned off by others with a transition, until when.
        self._member_turning_off_until: dict[str, float] = {}
        self._unsub_members: CALLBACK_TYPE | None = None
        self._commander: RoomCommander | None = None
        self._attr_color_mode = ColorMode.ONOFF
        self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def members(self) -> list[str]:
        """Return the lights of this room."""
        return self._members

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the members, which the frontend shows like a group."""
        return {ATTR_ENTITY_ID: self._members}

    @override
    async def async_added_to_hass(self) -> None:
        """Start following the member lights."""
        self._commander = RoomCommander(
            self.hass, COMMAND_INTERVAL_SECONDS, self._offsets, self._separate
        )
        self._async_subscribe_members()
        self._async_update_state()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop following the member lights.

        The manager keeps this object: Home Assistant adds it again after its
        entity id was changed.
        """
        if self._unsub_members is not None:
            self._unsub_members()
            self._unsub_members = None
        if self._commander is not None:
            self._commander.async_cancel()

    @callback
    def async_set_members(self, members: list[str]) -> None:
        """Apply a new member list."""
        if members == self._members:
            return
        self._members = members
        if self._unsub_members is not None:
            self._async_subscribe_members()
            self._async_update_state()
            self.async_write_ha_state()

    @callback
    def _async_subscribe_members(self) -> None:
        if self._unsub_members is not None:
            self._unsub_members()
        self._unsub_members = async_track_state_change_event(
            self.hass, self._members, self._async_member_changed
        )

    @callback
    def _async_member_changed(self, event: Event[EventStateChangedData]) -> None:
        was_on = self._attr_is_on
        self._async_update_state()
        self.async_write_ha_state()

        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if old_state is None or new_state is None:
            return
        automation = self._automation
        commander = self._commander
        if (
            automation is not None
            and commander is not None
            and was_on
            # Lights that became unavailable, e.g. during a restart, did not
            # turn off.
            and self._attr_is_on is False
            # A turn on right after the turn off may already be on its way.
            and not commander.turning_on
        ):
            automation.async_room_off()
        now = self.hass.loop.time()
        entity_id = new_state.entity_id
        if old_state.state == STATE_ON and new_state.state != STATE_ON:
            self._turned_off_at[entity_id] = now
            self._member_turning_off_until.pop(entity_id, None)
            if commander is not None:
                commander.async_forget_sent([entity_id])
        elif (
            automation is not None
            and commander is not None
            and old_state.state == STATE_ON
            and new_state.state == STATE_ON
            and not commander.is_own_context(new_state.context)
            # Lights report their values while fading out, too.
            and not self._turning_off(now)
            and now >= self._member_turning_off_until.get(entity_id, 0)
            and (attributes := self._changed_elsewhere(new_state, now))
        ):
            automation.async_mark_manual(attributes, command=False)
        elif (
            self._automation is not None
            and self._commander is not None
            and old_state.state in (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN)
            and new_state.state == STATE_ON
            # Our own commands already turned it on with the target.
            and not self._commander.is_own_context(new_state.context)
            # Lights may report on for a moment while turning off, also when
            # turned off without the room light.
            and not self._turning_off(now)
            and now - self._turned_off_at.get(new_state.entity_id, -LIGHT_OFF_GRACE)
            >= LIGHT_OFF_GRACE
        ):
            self._automation.async_light_turned_on(new_state.entity_id)

    def _changed_elsewhere(self, state: State, now: float) -> set[str]:
        """Return what changed on a light compared with what we last sent.

        For changes outside Home Assistant, e.g. by a remote bound to the
        light. Values reported during our own transitions do not count.
        """
        assert self._commander is not None
        sent = self._commander.last_sent.get(state.entity_id) or {}
        attributes = state.attributes
        changed = set()

        if (
            (sent_kelvin := sent.get(ATTR_COLOR_TEMP_KELVIN)) is not None
            and sent_kelvin.settled(now, TRANSITION_GRACE_SECONDS)
            and ColorMode.COLOR_TEMP
            in (attributes.get(ATTR_SUPPORTED_COLOR_MODES) or ())
        ):
            kelvin = attributes.get(ATTR_COLOR_TEMP_KELVIN)
            if (
                attributes.get(ATTR_COLOR_MODE) != ColorMode.COLOR_TEMP
                or kelvin is None
                or abs(kelvin - reachable_kelvin(sent_kelvin.value, attributes))
                >= MANUAL_COLOR_TEMP_THRESHOLD_KELVIN
            ):
                changed.add(PAUSE_COLOR_TEMP)
        if (
            (sent_brightness := sent.get(ATTR_BRIGHTNESS)) is not None
            and sent_brightness.settled(now, TRANSITION_GRACE_SECONDS)
            and (brightness := attributes.get(ATTR_BRIGHTNESS)) is not None
            and abs(brightness - sent_brightness.value) >= MANUAL_BRIGHTNESS_THRESHOLD
        ):
            changed.add(PAUSE_BRIGHTNESS)
        return changed

    @callback
    def async_member_turning_off(self, entity_id: str, transition: float) -> None:
        """Note a light that others turn off, which may fade out for a while."""
        self._member_turning_off_until[entity_id] = (
            self.hass.loop.time() + transition + TRANSITION_GRACE_SECONDS
        )

    @callback
    def async_take_shown(self) -> None:
        """Compare later changes with what the lights show now."""
        if self._commander is not None:
            self._commander.async_take_shown(self._members)

    @property
    def turning_on(self) -> bool:
        """Return True if a turn on of the room waits to be sent."""
        return self._commander is not None and self._commander.turning_on

    def is_own_context(self, context: Context) -> bool:
        """Return True if a context belongs to a command of this room."""
        return self._commander is not None and self._commander.is_own_context(context)

    def _turning_off(self, now: float) -> bool:
        """Return True while the room light is turning the room off."""
        assert self._commander is not None
        return now < self._turning_off_until or self._commander.turning_off

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on all lights of the room.

        Following the schedule, lights that are off turn on with the target
        right away, so they do not show their old color first.
        """
        assert self._commander is not None
        states = {
            entity_id: self.hass.states.get(entity_id) for entity_id in self._members
        }
        off = [
            entity_id
            for entity_id, state in states.items()
            if state is None or state.state != STATE_ON
        ]
        members = self._members
        if not kwargs and not self._turning_off(self.hass.loop.time()):
            # A bare turn on would not change lights that are on. Right after
            # turning off, they may still report on, though.
            members = off
        overrides = None
        automation = self._automation
        if automation is not None and (attributes := manual_attributes(kwargs)):
            automation.async_mark_manual(attributes)
        if (
            automation is not None
            and automation.active
            and automation.target is not None
            and COLOR_GROUP_ATTRS.isdisjoint(kwargs)
            and ATTR_EFFECT not in kwargs
            and ATTR_FLASH not in kwargs
        ):
            overrides = {
                entity_id: params
                for entity_id in off
                if (
                    params := _target_params(
                        states[entity_id], automation.target, kwargs, automation.paused
                    )
                )
            }
        if members:
            await self._commander.async_turn_on(
                members, kwargs, self._context, overrides
            )

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off all lights of the room."""
        assert self._commander is not None
        params = turn_off_params(kwargs)
        self._turning_off_until = (
            self.hass.loop.time() + params.get(ATTR_TRANSITION, 0) + TURNING_OFF_GRACE
        )
        await self._commander.async_turn_off(self._members, params, self._context)

    async def async_apply_target(
        self,
        target: Target,
        transition: float,
        *,
        members: list[str] | None = None,
        threshold: float = 0,
        paused: frozenset[str] = frozenset(),
    ) -> None:
        """Bring lights that are on and show a color temperature to the target.

        Lights showing a color were set by someone and are left alone. A light
        is compared to what it can show, with its offset and limited to its
        range, so lights that cannot reach the target are not sent it again
        and again. Paused attributes are left alone, and nothing is sent while
        the room is turning off.
        """
        assert self._commander is not None
        if self._turning_off(self.hass.loop.time()):
            return
        member_params: MemberParams = {}
        for entity_id in members or self._members:
            state = self.hass.states.get(entity_id)
            if state is None or state.state != STATE_ON:
                continue
            attributes = state.attributes
            # Lights showing a color, or only brightness, are left alone.
            if attributes.get(ATTR_COLOR_MODE) != ColorMode.COLOR_TEMP:
                continue
            params: dict[str, Any] = {}
            if (
                PAUSE_COLOR_TEMP not in paused
                and (current := attributes.get(ATTR_COLOR_TEMP_KELVIN)) is not None
            ):
                offset = self._offsets.get(entity_id)
                reachable = reachable_kelvin(
                    offset.apply(target.kelvin) if offset else target.kelvin,
                    attributes,
                )
                if abs(current - reachable) >= threshold:
                    params[ATTR_COLOR_TEMP_KELVIN] = target.kelvin
            if target.brightness is not None and PAUSE_BRIGHTNESS not in paused:
                brightness = _brightness_255(target.brightness)
                current = attributes.get(ATTR_BRIGHTNESS)
                if current is None or abs(current - brightness) >= BRIGHTNESS_THRESHOLD:
                    params[ATTR_BRIGHTNESS] = brightness
            if not params:
                continue
            if transition and attributes.get(ATTR_SUPPORTED_FEATURES, 0) & (
                LightEntityFeature.TRANSITION
            ):
                params[ATTR_TRANSITION] = transition
            member_params[entity_id] = params
        if member_params:
            await self._commander.async_send_now(member_params)

    @callback
    def _async_update_state(self) -> None:
        """Derive the room state from the member lights."""
        states = [
            state
            for entity_id in self._members
            if (state := self.hass.states.get(entity_id)) is not None
        ]
        on_states = [state for state in states if state.state == STATE_ON]

        self._attr_available = any(state.state != STATE_UNAVAILABLE for state in states)
        if on_states:
            self._attr_is_on = True
        elif all(state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE) for state in states):
            self._attr_is_on = None
        else:
            self._attr_is_on = False

        member_modes = [
            set(state.attributes.get(ATTR_SUPPORTED_COLOR_MODES) or ())
            for state in states
        ]
        supports_color = any(color_supported(modes) for modes in member_modes)
        supports_color_temp = any(
            ColorMode.COLOR_TEMP in modes for modes in member_modes
        )
        # Every color capable member converts hs_color into its own color space,
        # so the room only needs to offer HS and color temperature.
        if supports_color or supports_color_temp:
            supported_modes = set()
            if supports_color:
                supported_modes.add(ColorMode.HS)
            if supports_color_temp:
                supported_modes.add(ColorMode.COLOR_TEMP)
        elif any(brightness_supported(modes) for modes in member_modes):
            supported_modes = {ColorMode.BRIGHTNESS}
        else:
            supported_modes = {ColorMode.ONOFF}
        self._attr_supported_color_modes = supported_modes

        self._attr_brightness = _mean(
            state.attributes.get(ATTR_BRIGHTNESS) for state in on_states
        )
        color_states = [
            state
            for state in on_states
            if state.attributes.get(ATTR_COLOR_MODE) in COLOR_MODES_COLOR
        ]
        color_temp_states = [
            state
            for state in on_states
            if state.attributes.get(ATTR_COLOR_MODE) == ColorMode.COLOR_TEMP
        ]
        self._attr_hs_color = next(
            (
                state.attributes[ATTR_HS_COLOR]
                for state in color_states
                if state.attributes.get(ATTR_HS_COLOR) is not None
            ),
            None,
        )
        self._attr_color_mode = _room_color_mode(
            supported_modes, len(color_states), len(color_temp_states)
        )

        self._attr_min_color_temp_kelvin = min(
            (
                state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN]
                for state in states
                if state.attributes.get(ATTR_MIN_COLOR_TEMP_KELVIN)
            ),
            default=2000,
        )
        self._attr_max_color_temp_kelvin = max(
            (
                state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN]
                for state in states
                if state.attributes.get(ATTR_MAX_COLOR_TEMP_KELVIN)
            ),
            default=6500,
        )
        # Without its offset, a light at the edge of its range can be outside
        # the range of the room.
        color_temp = _mean(
            self._requested_color_temp(state) for state in color_temp_states
        )
        self._attr_color_temp_kelvin = (
            None
            if color_temp is None
            else min(
                max(color_temp, self._attr_min_color_temp_kelvin),
                self._attr_max_color_temp_kelvin,
            )
        )

        features = LightEntityFeature(0)
        for state in states:
            features |= state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        self._attr_supported_features = features & FORWARDED_FEATURES

        effects: set[str] = set()
        for state in states:
            effects.update(state.attributes.get(ATTR_EFFECT_LIST) or ())
        self._attr_effect_list = sorted(effects) if effects else None
        effect_counts = Counter(
            effect
            for state in on_states
            if (effect := state.attributes.get(ATTR_EFFECT)) in effects
        )
        self._attr_effect = (
            effect_counts.most_common(1)[0][0] if effect_counts else None
        )

    def _requested_color_temp(self, state: State) -> float | None:
        """Return the color temperature a light shows, without its offset."""
        if (kelvin := state.attributes.get(ATTR_COLOR_TEMP_KELVIN)) is None:
            return None
        if self._commander is not None and (
            sent := self._commander.sent_color_temps.get(state.entity_id)
        ):
            requested, sent_kelvin = sent
            # The sent value may have been limited to the light's range.
            if same_color_temp(kelvin, sent_kelvin):
                return requested
            # The light was changed some other way since.
            del self._commander.sent_color_temps[state.entity_id]
        if (offset := self._offsets.get(state.entity_id)) is not None:
            return offset.remove(kelvin)
        return kelvin

    def requested_color_temp(self, entity_id: str) -> float | None:
        """Return what a light that is on shows without its offset.

        None if the light is off or shows a color.
        """
        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state != STATE_ON
            or state.attributes.get(ATTR_COLOR_MODE) != ColorMode.COLOR_TEMP
        ):
            return None
        return self._requested_color_temp(state)

    async def async_set_member_color_temp(self, entity_id: str, kelvin: float) -> None:
        """Send a color temperature to one light, with its current offset.

        Sent right away, apart from the room's commands, which it must not be
        merged with.
        """
        assert self._commander is not None
        await self._commander.async_send_now(
            {entity_id: {ATTR_COLOR_TEMP_KELVIN: round(kelvin)}}
        )


def _target_params(
    state: State | None,
    target: Target,
    call_params: Mapping[str, Any],
    paused: set[str],
) -> dict[str, Any]:
    """Return what a light turning on needs to show the target.

    Brightness from the call wins over the target's brightness, and paused
    attributes are left out.
    """
    modes = (state.attributes.get(ATTR_SUPPORTED_COLOR_MODES) if state else None) or ()
    params: dict[str, Any] = {}
    if PAUSE_COLOR_TEMP not in paused and (
        state is None or ColorMode.COLOR_TEMP in modes
    ):
        params[ATTR_COLOR_TEMP_KELVIN] = target.kelvin
    if (
        target.brightness is not None
        and PAUSE_BRIGHTNESS not in paused
        and ATTR_BRIGHTNESS not in call_params
        and (state is None or brightness_supported(modes))
    ):
        params[ATTR_BRIGHTNESS] = _brightness_255(target.brightness)
    return params


def _brightness_255(percent: int) -> int:
    return value_to_brightness((1, 100), percent)


def _mean(values: Any) -> int | None:
    """Return the rounded mean of all values that are not None."""
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers))


def _room_color_mode(
    supported_modes: set[ColorMode], color_count: int, color_temp_count: int
) -> ColorMode:
    """Return the color mode most of the lights that are on are in."""
    if ColorMode.HS in supported_modes and (
        color_count > color_temp_count or ColorMode.COLOR_TEMP not in supported_modes
    ):
        return ColorMode.HS
    if ColorMode.COLOR_TEMP in supported_modes:
        return ColorMode.COLOR_TEMP
    return next(iter(supported_modes))
