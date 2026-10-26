"""One light entity per area."""

from collections import Counter
from collections.abc import Mapping
import logging
from typing import Any, override

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_HS_COLOR,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_SUPPORTED_COLOR_MODES,
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
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    COMMAND_INTERVAL_SECONDS,
)
from .manager import LightBuddyConfigEntry
from .offset import ColorTempOffset, same_color_temp
from .room import RoomCommander, turn_off_params

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

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
    ) -> None:
        """Initialize the room light."""
        self.area_id = area_id
        self.area_name = name
        self.entity_id = f"{LIGHT_DOMAIN}.all_{area_id}"
        self._attr_unique_id = area_id
        self._attr_translation_placeholders = {"area": name}
        self._members = members
        self._offsets = offsets
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
            self.hass, COMMAND_INTERVAL_SECONDS, self._offsets
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
    def _async_member_changed(self, _event: Event[Any]) -> None:
        self._async_update_state()
        self.async_write_ha_state()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on all lights of the room."""
        assert self._commander is not None
        await self._commander.async_turn_on(self._members, kwargs, self._context)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off all lights of the room."""
        assert self._commander is not None
        await self._commander.async_turn_off(
            self._members, turn_off_params(kwargs), self._context
        )

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
            [entity_id], {ATTR_COLOR_TEMP_KELVIN: round(kelvin)}
        )


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
