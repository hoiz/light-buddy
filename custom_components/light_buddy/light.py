"""One light entity per area."""

from collections import Counter
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
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    COMMAND_INTERVAL_SECONDS,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    MEMBERSHIP_DEBOUNCE_SECONDS,
)
from .membership import async_resolve_rooms
from .room import RoomCommander, turn_off_params

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

FORWARDED_FEATURES = (
    LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one room light per area with lights."""
    RoomManager(hass, entry, async_add_entities).async_setup()


class RoomManager:
    """Keep the room lights in sync with the area, device and entity registry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self._async_add_entities = async_add_entities
        self._lights: dict[str, RoomLight] = {}
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=MEMBERSHIP_DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_refresh,
        )

    @callback
    def async_setup(self) -> None:
        """Create the room lights and listen for registry changes."""
        self.entry.async_on_unload(self._debouncer.async_shutdown)
        for event_type, event_filter in (
            (er.EVENT_ENTITY_REGISTRY_UPDATED, _entity_event_filter),
            (dr.EVENT_DEVICE_REGISTRY_UPDATED, _device_event_filter),
            (ar.EVENT_AREA_REGISTRY_UPDATED, None),
        ):
            self.entry.async_on_unload(
                self.hass.bus.async_listen(
                    event_type, self._async_schedule_refresh, event_filter=event_filter
                )
            )
        self.async_refresh()

    @callback
    def _async_schedule_refresh(self, _event: Event[Any]) -> None:
        self._debouncer.async_schedule_call()

    @callback
    def async_refresh(self) -> None:
        """Add, update and remove room lights to match the areas."""
        rooms = async_resolve_rooms(
            self.hass,
            self.entry.options.get(CONF_EXCLUDED_AREAS, ()),
            self.entry.options.get(CONF_EXCLUDED_LIGHTS, ()),
        )
        area_registry = ar.async_get(self.hass)
        rooms = {
            area_id: members
            for area_id, members in rooms.items()
            if area_registry.async_get_area(area_id) is not None
        }

        entity_registry = er.async_get(self.hass)
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, self.entry.entry_id
        ):
            if registry_entry.unique_id not in rooms:
                self._lights.pop(registry_entry.unique_id, None)
                entity_registry.async_remove(registry_entry.entity_id)
        for area_id in self._lights.keys() - rooms.keys():
            self.hass.async_create_task(self._lights.pop(area_id).async_remove())

        new_lights: list[RoomLight] = []
        for area_id, members in rooms.items():
            name = area_registry.async_get_area(area_id).name  # type: ignore[union-attr]
            light = self._lights.get(area_id)
            if light is not None and light.area_name == name:
                light.async_set_members(members)
                continue
            new_light = RoomLight(area_id, name, members, self._async_light_removed)
            self._lights[area_id] = new_light
            if light is None:
                new_lights.append(new_light)
            else:
                # Home Assistant caches entity names, so a renamed area needs
                # a new entity. The registry entry and entity id are kept.
                self.entry.async_create_task(
                    self.hass, self._async_replace(light, new_light)
                )
        if new_lights:
            self._async_add_entities(new_lights)

    async def _async_replace(self, old: RoomLight, new: RoomLight) -> None:
        await old.async_remove()
        self._async_add_entities([new])

    @callback
    def _async_light_removed(self, light: RoomLight) -> None:
        if self._lights.get(light.area_id) is light:
            del self._lights[light.area_id]


@callback
def _entity_event_filter(event_data: er.EventEntityRegistryUpdatedData) -> bool:
    """Only light entities can change room membership."""
    return event_data["entity_id"].startswith(f"{LIGHT_DOMAIN}.")


@callback
def _device_event_filter(event_data: dr.EventDeviceRegistryUpdatedData) -> bool:
    """Devices matter when they are added, removed or moved to another area."""
    return event_data["action"] != "update" or "area_id" in event_data["changes"]


class RoomLight(LightEntity):
    """A light controlling all lights of an area."""

    _attr_available = False
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "room"

    def __init__(
        self,
        area_id: str,
        name: str,
        members: list[str],
        on_remove: Any,
    ) -> None:
        """Initialize the room light."""
        self.area_id = area_id
        self.area_name = name
        self.entity_id = f"{LIGHT_DOMAIN}.all_{area_id}"
        self._attr_unique_id = area_id
        self._attr_translation_placeholders = {"area": name}
        self._members = members
        self._removed_callback = on_remove
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
        self._commander = RoomCommander(self.hass, COMMAND_INTERVAL_SECONDS)
        self._async_subscribe_members()
        self._async_update_state()

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Stop following the member lights."""
        if self._unsub_members is not None:
            self._unsub_members()
            self._unsub_members = None
        if self._commander is not None:
            self._commander.async_cancel()
        self._removed_callback(self)

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
        self._attr_color_temp_kelvin = _mean(
            state.attributes.get(ATTR_COLOR_TEMP_KELVIN) for state in color_temp_states
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
