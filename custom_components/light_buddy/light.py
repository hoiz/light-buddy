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
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    COLOR_MODES_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    brightness_supported,
    color_supported,
    color_temp_supported,
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

from .automation import RoomAutomation, sets_color
from .const import (
    COMMAND_INTERVAL_SECONDS,
    MANUAL_COLOR_TEMP_THRESHOLD_KELVIN,
    TRANSITION_GRACE_SECONDS,
)
from .disco import restored_params
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

# The attribute that restores a light's color, per color mode.
RESTORED_COLOR_ATTRS = {
    ColorMode.HS: ATTR_HS_COLOR,
    ColorMode.XY: ATTR_XY_COLOR,
    ColorMode.RGB: ATTR_RGB_COLOR,
    ColorMode.RGBW: ATTR_RGBW_COLOR,
    ColorMode.RGBWW: ATTR_RGBWW_COLOR,
}

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
        # What lights showed when last changed outside Home Assistant.
        self._manual_color: dict[str, ShownColor] = {}
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
        if (
            automation is not None
            and commander is not None
            and {old_state.state, new_state.state} == {STATE_ON, STATE_OFF}
            and not commander.is_own_context(new_state.context)
        ):
            automation.disco.async_light_switched(
                entity_id, new_state.state == STATE_ON
            )
        if old_state.state == STATE_ON and new_state.state != STATE_ON:
            self._turned_off_at[entity_id] = now
            self._manual_color.pop(entity_id, None)
            if commander is not None:
                commander.async_forget_sent([entity_id])
        elif (
            automation is not None
            and commander is not None
            and old_state.state == STATE_ON
            and new_state.state == STATE_ON
            # Disco sets the colors, and leaves the schedule as it was.
            and not automation.disco.running
            and not commander.is_own_context(new_state.context)
            # Lights report their values while the room fades out, too.
            and not self._turning_off(now)
            and self._changed_elsewhere(new_state, now)
        ):
            automation.async_mark_manual(new_change=self._is_new_change(new_state))
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

    def _changed_elsewhere(self, state: State, now: float) -> bool:
        """Return True if a light's color changed from what we last sent.

        For changes outside Home Assistant, e.g. by a remote bound to the
        light. Values reported during our own transitions do not count.
        """
        assert self._commander is not None
        attributes = state.attributes
        sent = self._commander.last_sent.get(state.entity_id)
        return (
            sent is not None
            and sent.settled(now, TRANSITION_GRACE_SECONDS)
            and color_temp_supported(attributes.get(ATTR_SUPPORTED_COLOR_MODES))
            and _differs(
                (ColorMode.COLOR_TEMP, reachable_kelvin(sent.value, attributes)),
                _shown(attributes),
            )
        )

    def _is_new_change(self, state: State) -> bool:
        """Return True if a light's color changed since its last change by hand.

        A light keeps reporting the difference to what we sent, which is no
        new change. Compared with the start of the change, also slow ones
        count, like turning a remote's color step by step.
        """
        shown = _shown(state.attributes)
        last = self._manual_color.get(state.entity_id)
        if last is not None and not _differs(last, shown):
            return False
        self._manual_color[state.entity_id] = shown
        return True

    @callback
    def async_take_shown(self) -> None:
        """Compare later changes with what the lights show now."""
        self._manual_color.clear()
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
        turning_off = self._turning_off(self.hass.loop.time())
        # A turn on ends an earlier fade out of ours.
        self._turning_off_until = 0.0
        states = {
            entity_id: self.hass.states.get(entity_id) for entity_id in self._members
        }
        off = [
            entity_id
            for entity_id, state in states.items()
            if state is None or state.state != STATE_ON
        ]
        members = self._members
        if not kwargs and not turning_off:
            # A bare turn on would not change lights that are on. Right after
            # turning off, they may still report on, though.
            members = off
        overrides = None
        automation = self._automation
        if automation is not None and automation.disco.running:
            if sets_color(kwargs):
                # A color of its own ends disco, the lights keep it.
                await automation.disco.async_stop(restore=False)
            else:
                # White lights stay off during disco.
                colored = set(self.color_members)
                members = [entity_id for entity_id in members if entity_id in colored]
        if automation is not None and sets_color(kwargs):
            automation.async_mark_manual()
        if (
            automation is not None
            and automation.active
            and not automation.paused
            and automation.target is not None
            and COLOR_GROUP_ATTRS.isdisjoint(kwargs)
            and ATTR_EFFECT not in kwargs
            and ATTR_FLASH not in kwargs
        ):
            # Lights that cannot show a color temperature turn on as asked.
            target = {ATTR_COLOR_TEMP_KELVIN: automation.target.kelvin}
            overrides = {
                entity_id: target
                for entity_id in off
                if (state := states[entity_id]) is None
                or color_temp_supported(
                    state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)
                )
            }
        if members:
            await self._commander.async_turn_on(
                members, kwargs, self._context, overrides
            )

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off all lights of the room. Disco ends, the lights stay off."""
        assert self._commander is not None
        if self._automation is not None:
            await self._automation.disco.async_stop(restore=False)
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
    ) -> None:
        """Bring lights that are on and show a color temperature to the target.

        Lights showing a color were set by someone and are left alone. A light
        is compared to what it can show, with its offset and limited to its
        range, so lights that cannot reach the target are not sent it again
        and again. Nothing is sent while the room is turning off.
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
            if (
                attributes.get(ATTR_COLOR_MODE) != ColorMode.COLOR_TEMP
                or (current := attributes.get(ATTR_COLOR_TEMP_KELVIN)) is None
            ):
                continue
            offset = self._offsets.get(entity_id)
            reachable = reachable_kelvin(
                offset.apply(target.kelvin) if offset else target.kelvin, attributes
            )
            if abs(current - reachable) < threshold:
                continue
            params: dict[str, Any] = {ATTR_COLOR_TEMP_KELVIN: target.kelvin}
            if transition and attributes.get(ATTR_SUPPORTED_FEATURES, 0) & (
                LightEntityFeature.TRANSITION
            ):
                params[ATTR_TRANSITION] = transition
            member_params[entity_id] = params
        if member_params:
            await self._commander.async_send_now(member_params)

    @property
    def color_members(self) -> list[str]:
        """Return the lights of this room that can show colors, sorted."""
        return sorted(
            entity_id
            for entity_id in self._members
            if (state := self.hass.states.get(entity_id)) is not None
            and color_supported(state.attributes.get(ATTR_SUPPORTED_COLOR_MODES))
        )

    @callback
    def async_snapshot(self) -> dict[str, dict[str, Any] | None]:
        """Return the params that bring each light back to what it shows.

        None for lights that are off. Color temperatures are without offset,
        which sending applies again. Lights that are unavailable are left out.

        Colored lights in an unknown state count as off: disco turns them on,
        and they are usually off, e.g. after a restart before they reported.
        """
        snapshot: dict[str, dict[str, Any] | None] = {}
        for entity_id in self._members:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if state.state == STATE_OFF or (
                state.state == STATE_UNKNOWN
                and color_supported(state.attributes.get(ATTR_SUPPORTED_COLOR_MODES))
            ):
                snapshot[entity_id] = None
                continue
            if state.state != STATE_ON:
                continue
            attributes = state.attributes
            params: dict[str, Any] = {}
            if (brightness := attributes.get(ATTR_BRIGHTNESS)) is not None:
                params[ATTR_BRIGHTNESS] = brightness
            color_mode = attributes.get(ATTR_COLOR_MODE)
            if color_mode == ColorMode.COLOR_TEMP:
                if (kelvin := self._requested_color_temp(state)) is not None:
                    params[ATTR_COLOR_TEMP_KELVIN] = round(kelvin)
            elif (attr := RESTORED_COLOR_ATTRS.get(color_mode)) is not None and (
                value := attributes.get(attr)
            ) is not None:
                params[attr] = value
            snapshot[entity_id] = params
        return snapshot

    async def async_disco_start(
        self, colors: Mapping[str, tuple[float, float]]
    ) -> None:
        """Give the colored lights their first color, and turn the others off.

        Lights that are off turn on with the room's brightness, or full
        brightness if the room is off.
        """
        assert self._commander is not None
        brightness = self.brightness if self.is_on and self.brightness else 255
        member_params: MemberParams = {}
        for entity_id, hs_color in colors.items():
            params: dict[str, Any] = {ATTR_HS_COLOR: hs_color}
            if (
                state := self.hass.states.get(entity_id)
            ) is None or state.state != STATE_ON:
                params[ATTR_BRIGHTNESS] = brightness
            member_params[entity_id] = params
        # Colored lights first, so the room does not look off in between.
        await self._commander.async_send_now(member_params)
        if white := [
            entity_id
            for entity_id in self._members
            if entity_id not in colors
            and (state := self.hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
        ]:
            await self._commander.async_turn_off_now(white)

    async def async_disco_step(
        self, colors: Mapping[str, tuple[float, float]], transition: float
    ) -> None:
        """Give the colored lights that are on their next color.

        Lights turned off meanwhile, e.g. at their own switch, stay off.
        """
        assert self._commander is not None
        member_params: MemberParams = {}
        for entity_id, hs_color in colors.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.state != STATE_ON:
                continue
            params: dict[str, Any] = {ATTR_HS_COLOR: hs_color}
            if transition and state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) & (
                LightEntityFeature.TRANSITION
            ):
                params[ATTR_TRANSITION] = transition
            member_params[entity_id] = params
        if member_params:
            await self._commander.async_send_now(member_params)

    async def async_restore(
        self,
        snapshot: Mapping[str, dict[str, Any] | None],
        color_temp: float | None,
    ) -> None:
        """Bring the lights back to a snapshot.

        With a color temperature, lights that showed one get it instead, so
        lights following the schedule get its current target.
        """
        assert self._commander is not None
        on: MemberParams = {}
        off: list[str] = []
        for entity_id, params in snapshot.items():
            if entity_id not in self._members:
                continue
            if params is None:
                off.append(entity_id)
            else:
                on[entity_id] = restored_params(params, color_temp)
        try:
            if on:
                await self._commander.async_send_now(on)
        finally:
            if off:
                await self._commander.async_turn_off_now(off)

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
        supports_color_temp = any(color_temp_supported(modes) for modes in member_modes)
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


type ShownColor = tuple[str | None, float | None]


def _shown(attributes: Mapping[str, Any]) -> ShownColor:
    """Return the color mode and color temperature a light shows."""
    return (attributes.get(ATTR_COLOR_MODE), attributes.get(ATTR_COLOR_TEMP_KELVIN))


def _differs(value: ShownColor, other: ShownColor) -> bool:
    """Return True if two colors of a light look different.

    A change of color mode counts, too.
    """
    (mode, kelvin), (other_mode, other_kelvin) = value, other
    if mode != other_mode:
        return True
    if kelvin is None or other_kelvin is None:
        return (kelvin is None) != (other_kelvin is None)
    return abs(kelvin - other_kelvin) >= MANUAL_COLOR_TEMP_THRESHOLD_KELVIN


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
