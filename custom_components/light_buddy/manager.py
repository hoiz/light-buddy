"""Keep room lights and offset sliders in sync with the registries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_SUPPORTED_COLOR_MODES,
    DOMAIN as LIGHT_DOMAIN,
    color_supported,
    color_temp_supported,
)
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_OFFSETS,
    MEMBERSHIP_DEBOUNCE_SECONDS,
)
from .membership import async_resolve_rooms
from .offset import async_resolve_offsets

if TYPE_CHECKING:
    from .light import RoomLight
    from .number import ColorTempOffsetNumber

_LOGGER = logging.getLogger(__name__)

type LightBuddyConfigEntry = ConfigEntry[RoomManager]

# Name changes rename the offset sliders.
MEMBERSHIP_FIELDS = frozenset({"area_id", "device_id", "disabled_by", "name"})


class RoomManager:
    """Keep the room lights and offset sliders in sync with the registries."""

    def __init__(self, hass: HomeAssistant, entry: LightBuddyConfigEntry) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self.async_add_lights: AddConfigEntryEntitiesCallback | None = None
        self.async_add_numbers: AddConfigEntryEntitiesCallback | None = None
        # Offset in Kelvin per light entity id. Shared with all room lights,
        # and updated when entity ids change.
        self.offsets: dict[str, int] = {}
        self._lights: dict[str, RoomLight] = {}
        self._numbers: dict[str, ColorTempOffsetNumber] = {}
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=MEMBERSHIP_DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_refresh,
        )

    @callback
    def async_setup(self) -> None:
        """Create the entities and listen for registry changes."""
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
        """Add, update and remove entities to match the areas."""
        self.offsets.clear()
        self.offsets.update(
            async_resolve_offsets(self.hass, self.entry.options.get(CONF_OFFSETS, {}))
        )
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
        offset_lights = {
            entry.id: entry
            for members in rooms.values()
            for entity_id in members
            if (entry := entity_registry.async_get(entity_id)) is not None
            and _supports_color_temp(self.hass, entry)
        }

        wanted = {LIGHT_DOMAIN: rooms.keys(), NUMBER_DOMAIN: offset_lights.keys()}
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, self.entry.entry_id
        ):
            unique_ids = wanted.get(registry_entry.domain, ())
            key = registry_entry.unique_id.removesuffix(OFFSET_SUFFIX)
            if key not in unique_ids:
                self._lights.pop(key, None)
                self._numbers.pop(key, None)
                entity_registry.async_remove(registry_entry.entity_id)
        for area_id in self._lights.keys() - rooms.keys():
            self.entry.async_create_task(
                self.hass, _async_remove_entity(self._lights.pop(area_id))
            )
        for registry_id in self._numbers.keys() - offset_lights.keys():
            self.entry.async_create_task(
                self.hass, _async_remove_entity(self._numbers.pop(registry_id))
            )

        self._async_refresh_lights(rooms, area_registry)
        self._async_refresh_numbers(offset_lights)

    @callback
    def _async_refresh_lights(
        self, rooms: dict[str, list[str]], area_registry: ar.AreaRegistry
    ) -> None:
        from .light import RoomLight

        new_lights: list[RoomLight] = []
        for area_id, members in rooms.items():
            name = area_registry.async_get_area(area_id).name  # type: ignore[union-attr]
            light = self._lights.get(area_id)
            if light is not None and light.area_name == name:
                light.async_set_members(members)
                continue
            new_light = RoomLight(area_id, name, members, self.offsets)
            self._lights[area_id] = new_light
            if light is None:
                new_lights.append(new_light)
            else:
                # Home Assistant caches entity names, so a renamed area needs
                # a new entity. The registry entry and entity id are kept.
                self.entry.async_create_task(
                    self.hass,
                    self._async_replace(self._lights, area_id, light, new_light),
                )
        if new_lights and self.async_add_lights is not None:
            self.async_add_lights(new_lights)

    @callback
    def _async_refresh_numbers(
        self, offset_lights: dict[str, er.RegistryEntry]
    ) -> None:
        from .number import ColorTempOffsetNumber

        device_registry = dr.async_get(self.hass)
        new_numbers: list[ColorTempOffsetNumber] = []
        for registry_id, light_entry in offset_lights.items():
            device = (
                device_registry.async_get(light_entry.device_id)
                if light_entry.device_id
                else None
            )
            # Lights that are not the main entity of their device, or have no
            # device, need their own name in the slider's name.
            light_name = None
            if device is None or light_entry.name or light_entry.original_name:
                light_name = (
                    light_entry.name
                    or light_entry.original_name
                    or light_entry.entity_id
                )
            number = self._numbers.get(registry_id)
            if number is not None and number.matches(device, light_name):
                continue
            new_number = ColorTempOffsetNumber(self, light_entry, device, light_name)
            self._numbers[registry_id] = new_number
            if number is None:
                new_numbers.append(new_number)
            else:
                self.entry.async_create_task(
                    self.hass,
                    self._async_replace(self._numbers, registry_id, number, new_number),
                )
        if new_numbers and self.async_add_numbers is not None:
            self.async_add_numbers(new_numbers)

    async def _async_replace(
        self, entities: dict[str, Any], key: str, old: Any, new: Any
    ) -> None:
        await _async_remove_entity(old)
        # A later refresh may have removed or replaced the entity meanwhile.
        if entities.get(key) is not new:
            return
        add = (
            self.async_add_lights
            if entities is self._lights
            else self.async_add_numbers
        )
        if add is not None:
            add([new])

    def offset(self, registry_id: str) -> int:
        """Return the offset of a light by its registry id."""
        return self.entry.options.get(CONF_OFFSETS, {}).get(registry_id, 0)

    async def async_set_offset(self, registry_id: str, offset: int) -> None:
        """Store a light's offset and show it on the light right away."""
        entity_id = er.async_resolve_entity_id(er.async_get(self.hass), registry_id)
        if entity_id is None:
            return
        room = next(
            (light for light in self._lights.values() if entity_id in light.members),
            None,
        )
        # Taken before the offset changes, while it still refers to the old one.
        requested = room.requested_color_temp(entity_id) if room else None

        offsets = dict(self.entry.options.get(CONF_OFFSETS, {}))
        if offset:
            offsets[registry_id] = offset
            self.offsets[entity_id] = offset
        else:
            offsets.pop(registry_id, None)
            self.offsets.pop(entity_id, None)
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_OFFSETS: offsets}
        )

        if room is not None and requested is not None:
            await room.async_set_member_color_temp(entity_id, requested)


OFFSET_SUFFIX = "_color_temp_offset"


async def _async_remove_entity(entity: Any) -> None:
    """Remove an entity, unless it never was added, e.g. while disabled."""
    if entity.hass is not None:
        await entity.async_remove()


def _supports_color_temp(hass: HomeAssistant, entry: er.RegistryEntry) -> bool:
    """Return True if a light can show a color temperature.

    Lights with only colors convert color temperatures into a color.
    """
    modes = (entry.capabilities or {}).get(ATTR_SUPPORTED_COLOR_MODES)
    if modes is None and (state := hass.states.get(entry.entity_id)) is not None:
        modes = state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)
    return color_temp_supported(modes) or color_supported(modes)


@callback
def _entity_event_filter(event_data: er.EventEntityRegistryUpdatedData) -> bool:
    """Lights matter when they are added, removed, renamed or moved."""
    if not event_data["entity_id"].startswith(f"{LIGHT_DOMAIN}."):
        return False
    if event_data["action"] != "update":
        return True
    return "old_entity_id" in event_data or not MEMBERSHIP_FIELDS.isdisjoint(
        event_data["changes"]
    )


@callback
def _device_event_filter(event_data: dr.EventDeviceRegistryUpdatedData) -> bool:
    """Devices matter when they are added, removed or moved to another area."""
    return event_data["action"] != "update" or "area_id" in event_data["changes"]
