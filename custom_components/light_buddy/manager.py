"""Keep the entities of all rooms in sync with the registries."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_SUPPORTED_COLOR_MODES,
    DOMAIN as LIGHT_DOMAIN,
    color_temp_supported,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    SERVICE_TOGGLE,
    SERVICE_TURN_ON,
    STATE_ON,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)

from .automation import RoomAutomation, ScheduleSettings, sets_color
from .const import (
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_OFFSETS,
    CONF_SCHEDULE,
    CONF_SCHEDULES_OFF,
    MEMBERSHIP_DEBOUNCE_SECONDS,
    SEPARATE_ALL,
    SEPARATE_IKEA,
)
from .disco import DiscoSettings
from .membership import async_resolve_rooms
from .offset import ColorTempOffset
from .schedule import points_from_options

if TYPE_CHECKING:
    from .entity import LightOffsetEntity, RoomEntity
    from .light import RoomLight

_LOGGER = logging.getLogger(__name__)

type LightBuddyConfigEntry = ConfigEntry[RoomManager]
type ManagedEntity = RoomLight | RoomEntity | LightOffsetEntity

# Registry changes that can move a light between rooms, change whether it
# gets offset settings, or rename them.
REFRESH_FIELDS = frozenset(
    {"area_id", "capabilities", "device_id", "disabled_by", "name", "original_name"}
)


# Of the one sensor all rooms share.
TARGET_SENSOR_UNIQUE_ID = "target_color_temp"


class RoomManager:
    """Keep the entities of all rooms in sync with the registries.

    Each area with lights gets a room light, a schedule automation with its
    switch and sensor, and each of its lights with color temperature gets
    offset settings.
    """

    def __init__(self, hass: HomeAssistant, entry: LightBuddyConfigEntry) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.entry = entry
        self.async_add_entities: dict[Platform, AddConfigEntryEntitiesCallback] = {}
        # Offsets per entity id of lights with offset settings. Shared with all
        # room lights, and updated when entity ids change.
        self.offsets: dict[str, ColorTempOffset] = {}
        # Lights that get brightness and color as separate commands. Shared
        # with all room lights, too.
        self.separate_lights: set[str] = set()
        self.points = points_from_options(entry.options.get(CONF_SCHEDULE))
        self.settings = ScheduleSettings.from_options(entry.options)
        self.disco_settings = DiscoSettings.from_options(entry.options)
        self._exclusions = _exclusions(entry.options)
        self._lights: dict[str, RoomLight] = {}
        self._light_rooms: dict[str, str] = {}
        self._offset_light_ids: set[str] = set()
        self._offset_entities: dict[str, list[LightOffsetEntity]] = {}
        self._automations: dict[str, RoomAutomation] = {}
        self._room_entities: dict[str, list[RoomEntity]] = {}
        self._schedule_listeners: list[CALLBACK_TYPE] = []
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
        self.entry.async_on_unload(self._async_stop_automations)
        self.entry.async_on_unload(
            self.hass.bus.async_listen(
                EVENT_CALL_SERVICE,
                self._async_light_service_called,
                event_filter=_light_service_filter,
            )
        )
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
    def _async_light_service_called(self, event: Event[Mapping[str, Any]]) -> None:
        """Pause the schedule for colors others set on the lights of a room.

        Covers lights controlled directly, by automations, and by scenes,
        which apply their states through light.turn_on as well.
        """
        service_data = event.data[ATTR_SERVICE_DATA]
        if not sets_color(service_data):
            return
        # Groups are not expanded: a call to a room light is handled by it.
        selected = async_extract_referenced_entity_ids(
            self.hass, TargetSelection(service_data), expand_group=False
        )
        for entity_id in selected.referenced | selected.indirectly_referenced:
            if (area_id := self._light_rooms.get(entity_id)) is None:
                continue
            light = self._lights.get(area_id)
            if light is not None and light.is_own_context(event.context):
                continue
            # A toggle only turns lights that are off on, with its color.
            if event.data[ATTR_SERVICE] == SERVICE_TOGGLE and (
                (state := self.hass.states.get(entity_id)) is not None
                and state.state == STATE_ON
            ):
                continue
            if (automation := self._automations.get(area_id)) is not None:
                automation.async_mark_manual()

    @callback
    def async_refresh(self) -> None:
        """Add, update and remove entities to match the areas."""
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
        self._light_rooms = {
            entity_id: area_id
            for area_id, members in rooms.items()
            for entity_id in members
        }
        entity_registry = er.async_get(self.hass)
        offset_lights = {
            entry.id: entry
            for entity_id in self._light_rooms
            if (entry := entity_registry.async_get(entity_id)) is not None
            and _supports_color_temp(self.hass, entry)
        }
        # Only lights with offset settings get an offset, so none applies
        # without a way to see and change it. Lights without an offset are
        # left out, so they keep sharing service calls.
        self.offsets.clear()
        self.offsets.update(
            (entry.entity_id, offset)
            for registry_id, entry in offset_lights.items()
            if (offset := self.offset(registry_id)).value
        )
        self._offset_light_ids = {entry.entity_id for entry in offset_lights.values()}
        self._async_update_separate_lights()

        self._async_remove_stale(entity_registry, rooms.keys(), offset_lights.keys())
        self._async_refresh_automations(rooms.keys())
        self._async_refresh_lights(rooms, area_registry)
        self._async_refresh_room_entities(rooms.keys(), area_registry)
        self._async_refresh_offset_entities(offset_lights)

    @callback
    def _async_remove_stale(
        self,
        entity_registry: er.EntityRegistry,
        area_ids: Collection[str],
        registry_ids: Collection[str],
    ) -> None:
        """Remove the entities of areas and lights that are gone."""
        wanted = (
            {
                f"{area_id}{suffix}"
                for area_id in area_ids
                for suffix in (
                    "",
                    *(cls.UNIQUE_ID_SUFFIX for cls in _room_entity_classes()),
                )
            }
            | {
                f"{registry_id}{cls.UNIQUE_ID_SUFFIX}"
                for registry_id in registry_ids
                for cls in offset_entity_classes()
            }
            | {TARGET_SENSOR_UNIQUE_ID}
        )
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, self.entry.entry_id
        ):
            if registry_entry.unique_id not in wanted:
                entity_registry.async_remove(registry_entry.entity_id)

        # Removing from the registry removes added entities. Those never added,
        # e.g. while disabled, are only dropped.
        for area_id in self._lights.keys() - area_ids:
            self._async_replace([self._lights.pop(area_id)], [], lambda: False)
        for area_id in self._room_entities.keys() - area_ids:
            self._async_replace(self._room_entities.pop(area_id), [], lambda: False)
        for registry_id in self._offset_entities.keys() - registry_ids:
            self._async_replace(
                self._offset_entities.pop(registry_id), [], lambda: False
            )

    @callback
    def _async_refresh_automations(self, area_ids: Collection[str]) -> None:
        for area_id in self._automations.keys() - area_ids:
            self._automations.pop(area_id).async_stop()
        for area_id in area_ids:
            if area_id not in self._automations:
                automation = RoomAutomation(
                    self.hass,
                    area_id,
                    lambda area_id=area_id: self._lights.get(area_id),
                    area_id not in self.entry.options.get(CONF_SCHEDULES_OFF, ()),
                    self._async_store_schedule_enabled,
                )
                automation.async_set_points(self.points)
                automation.async_set_settings(self.settings)
                automation.async_set_disco_settings(self.disco_settings)
                self._automations[area_id] = automation
                automation.async_start()

    def needs_reload(self, options: Mapping[str, Any]) -> bool:
        """Return True if new options change which lights belong to rooms."""
        return _exclusions(options) != self._exclusions

    @callback
    def async_apply_options(self) -> None:
        """Follow a changed schedule and settings, without reloading.

        Options the entities store themselves, e.g. offsets, change neither.
        """
        points = points_from_options(self.entry.options.get(CONF_SCHEDULE))
        settings = ScheduleSettings.from_options(self.entry.options)
        disco_settings = DiscoSettings.from_options(self.entry.options)
        if disco_settings != self.disco_settings:
            self.disco_settings = disco_settings
            for automation in self._automations.values():
                automation.async_set_disco_settings(disco_settings)
        if points == self.points and settings == self.settings:
            return
        separate_changed = settings.separate_commands != self.settings.separate_commands
        # Only these change what the lights get.
        target_changed = (
            points != self.points
            or settings.threshold != self.settings.threshold
            or settings.transition != self.settings.transition
        )
        points_changed = points != self.points
        self.points, self.settings = points, settings
        if points_changed:
            for update in list(self._schedule_listeners):
                update()
        for automation in self._automations.values():
            automation.async_set_points(points)
            automation.async_set_settings(settings)
            if target_changed:
                automation.async_apply_soon()
        if separate_changed:
            self._async_update_separate_lights()

    @callback
    def async_add_schedule_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call update when the schedule changes."""
        self._schedule_listeners.append(update)
        return lambda: self._schedule_listeners.remove(update)

    @callback
    def _async_update_separate_lights(self) -> None:
        """Find the lights that get brightness and color separately."""
        mode = self.settings.separate_commands
        members = self._light_rooms.keys()
        if mode == SEPARATE_ALL:
            separate = set(members)
        elif mode == SEPARATE_IKEA:
            entity_registry = er.async_get(self.hass)
            device_registry = dr.async_get(self.hass)
            separate = {
                entity_id
                for entity_id in members
                if (entry := entity_registry.async_get(entity_id)) is not None
                and entry.device_id is not None
                and (device := device_registry.async_get(entry.device_id)) is not None
                and "ikea" in (device.manufacturer or "").lower()
            }
        else:
            separate = set()
        # Updated in place, the room lights share the set.
        self.separate_lights.clear()
        self.separate_lights.update(separate)

    @callback
    def _async_store_schedule_enabled(self, area_id: str, enabled: bool) -> None:
        schedules_off = set(self.entry.options.get(CONF_SCHEDULES_OFF, ()))
        if enabled:
            schedules_off.discard(area_id)
        else:
            schedules_off.add(area_id)
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_SCHEDULES_OFF: sorted(schedules_off)},
        )

    @callback
    def _async_stop_automations(self) -> None:
        for automation in self._automations.values():
            automation.async_stop()
        self._automations.clear()

    @callback
    def _async_refresh_lights(
        self, rooms: dict[str, list[str]], area_registry: ar.AreaRegistry
    ) -> None:
        from .light import RoomLight

        new_lights: list[ManagedEntity] = []
        for area_id, members in rooms.items():
            name = area_registry.async_get_area(area_id).name  # type: ignore[union-attr]
            light = self._lights.get(area_id)
            if light is not None and light.area_name == name:
                light.async_set_members(members)
                continue
            new_light = RoomLight(
                area_id,
                name,
                members,
                self.offsets,
                self._automations[area_id],
                self.separate_lights,
            )
            self._lights[area_id] = new_light
            if light is None:
                new_lights.append(new_light)
            else:
                # Home Assistant caches entity names, so a renamed area needs
                # a new entity. The registry entry and entity id are kept.
                self._async_replace(
                    [light],
                    [new_light],
                    lambda area_id=area_id, new=new_light: (
                        self._lights.get(area_id) is new
                    ),
                )
        self._async_add(new_lights)

    @callback
    def _async_refresh_room_entities(
        self, area_ids: Collection[str], area_registry: ar.AreaRegistry
    ) -> None:
        new_entities: list[ManagedEntity] = []
        for area_id in area_ids:
            name = area_registry.async_get_area(area_id).name  # type: ignore[union-attr]
            entities = self._room_entities.get(area_id)
            if entities is not None and all(
                entity.area_name == name for entity in entities
            ):
                continue
            automation = self._automations[area_id]
            new: list[RoomEntity] = [
                cls(automation, name) for cls in _room_entity_classes()
            ]
            self._room_entities[area_id] = new
            if entities is None:
                new_entities.extend(new)
            else:
                # Names come from the area, see the room lights.
                self._async_replace(
                    entities,
                    new,
                    lambda area_id=area_id, new=new: (
                        self._room_entities.get(area_id) is new
                    ),
                )
        self._async_add(new_entities)

    @callback
    def _async_refresh_offset_entities(
        self, offset_lights: dict[str, er.RegistryEntry]
    ) -> None:
        device_registry = dr.async_get(self.hass)
        new_entities: list[ManagedEntity] = []
        for registry_id, light_entry in offset_lights.items():
            device = (
                device_registry.async_get(light_entry.device_id)
                if light_entry.device_id
                else None
            )
            light_name = None
            if device is None or light_entry.name or light_entry.original_name:
                light_name = (
                    light_entry.name
                    or light_entry.original_name
                    or light_entry.entity_id
                )
            entities = self._offset_entities.get(registry_id)
            if entities is not None and all(
                entity.matches(device, light_name) for entity in entities
            ):
                continue
            new: list[LightOffsetEntity] = [
                cls(self, light_entry, device, light_name)
                for cls in offset_entity_classes()
            ]
            self._offset_entities[registry_id] = new
            if entities is None:
                new_entities.extend(new)
            else:
                self._async_replace(
                    entities,
                    new,
                    lambda registry_id=registry_id, new=new: (
                        self._offset_entities.get(registry_id) is new
                    ),
                )
        self._async_add(new_entities)

    @callback
    def _async_add(self, entities: Sequence[ManagedEntity]) -> None:
        """Add entities through the platform each of them belongs to."""
        for platform, add in self.async_add_entities.items():
            if batch := [entity for entity in entities if platform == entity.PLATFORM]:
                add(batch)

    @callback
    def _async_replace(
        self,
        old: Sequence[ManagedEntity],
        new: Sequence[ManagedEntity],
        is_current: Callable[[], bool],
    ) -> None:
        """Remove entities, then add their replacements if still wanted."""

        async def _async_replace() -> None:
            for entity in old:
                # Entities that were never added, e.g. while disabled, have no
                # hass to be removed from.
                if entity.hass is not None:
                    await entity.async_remove()
            # A later refresh may have removed or replaced them meanwhile.
            if is_current():
                self._async_add(new)

        self.entry.async_create_task(self.hass, _async_replace())

    def offset(self, registry_id: str) -> ColorTempOffset:
        """Return the offset of a light by its registry id."""
        if data := self.entry.options.get(CONF_OFFSETS, {}).get(registry_id):
            return ColorTempOffset.from_dict(data)
        return ColorTempOffset()

    async def async_set_offset(self, registry_id: str, offset: ColorTempOffset) -> None:
        """Store a light's offset and show it on the light right away."""
        entity_id = er.async_resolve_entity_id(er.async_get(self.hass), registry_id)
        if entity_id is None:
            return
        room = self._lights.get(self._light_rooms.get(entity_id, ""))
        if room is not None and room.hass is None:
            # Not added, e.g. because the room light is disabled.
            room = None
        # Taken before the offset changes, while it still refers to the old one.
        requested = room.requested_color_temp(entity_id) if room else None

        offsets = dict(self.entry.options.get(CONF_OFFSETS, {}))
        if offset == ColorTempOffset():
            offsets.pop(registry_id, None)
        else:
            offsets[registry_id] = offset.as_dict()
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_OFFSETS: offsets}
        )
        if entity_id in self._offset_light_ids:
            if offset.value:
                self.offsets[entity_id] = offset
            else:
                self.offsets.pop(entity_id, None)
        for entity in self._offset_entities.get(registry_id, ()):
            if entity.hass is not None:
                entity.async_write_ha_state()

        if room is not None and requested is not None:
            await room.async_set_member_color_temp(entity_id, requested)


def _room_entity_classes() -> tuple[type[RoomEntity], ...]:
    """Return the entities each room gets next to its room light."""
    # Imported here, as the platforms import this module.
    from .switch import DiscoSwitch, ScheduleSwitch

    return (ScheduleSwitch, DiscoSwitch)


def offset_entity_classes() -> tuple[type[LightOffsetEntity], ...]:
    """Return the offset settings each light with color temperature gets."""
    from .number import ColorTempOffsetNumber
    from .select import ColorTempOffsetUnitSelect

    return (ColorTempOffsetNumber, ColorTempOffsetUnitSelect)


def _supports_color_temp(hass: HomeAssistant, entry: er.RegistryEntry) -> bool:
    """Return True if a light can show a color temperature.

    Lights with only colors are left out: they show a color temperature as a
    color, which an offset cannot be previewed on.
    """
    modes = (entry.capabilities or {}).get(ATTR_SUPPORTED_COLOR_MODES)
    if modes is None and (state := hass.states.get(entry.entity_id)) is not None:
        modes = state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)
    return color_temp_supported(modes)


@callback
def _entity_event_filter(event_data: er.EventEntityRegistryUpdatedData) -> bool:
    """Lights matter when they are added, removed, renamed or changed."""
    if not event_data["entity_id"].startswith(f"{LIGHT_DOMAIN}."):
        return False
    if event_data["action"] != "update":
        return True
    return "old_entity_id" in event_data or not REFRESH_FIELDS.isdisjoint(
        event_data["changes"]
    )


def _exclusions(options: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Return the excluded areas and lights of options."""
    return (
        set(options.get(CONF_EXCLUDED_AREAS, ())),
        set(options.get(CONF_EXCLUDED_LIGHTS, ())),
    )


@callback
def _light_service_filter(event_data: Mapping[str, Any]) -> bool:
    """Only calls that turn lights on can set a color."""
    return event_data[ATTR_DOMAIN] == LIGHT_DOMAIN and event_data[ATTR_SERVICE] in (
        SERVICE_TURN_ON,
        SERVICE_TOGGLE,
    )


@callback
def _device_event_filter(event_data: dr.EventDeviceRegistryUpdatedData) -> bool:
    """Devices matter when added, removed, moved or their maker is known.

    The maker tells IKEA lights for separate commands.
    """
    return event_data["action"] != "update" or not {
        "area_id",
        "manufacturer",
    }.isdisjoint(event_data["changes"])
