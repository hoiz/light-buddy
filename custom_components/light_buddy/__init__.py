"""The Light Buddy integration: one light entity per area."""

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .manager import LightBuddyConfigEntry, RoomManager, offset_entity_classes

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: LightBuddyConfigEntry) -> bool:
    """Set up Light Buddy from a config entry."""
    entry.runtime_data = RoomManager(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.runtime_data.async_setup()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: LightBuddyConfigEntry
) -> None:
    """Apply changed options.

    Changed exclusions change the rooms, which needs a reload. The schedule
    and settings apply right away, without ending pauses. Offsets and
    switches store their own options and are applied already.
    """
    manager = entry.runtime_data
    if manager.needs_reload(entry.options):
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        manager.async_apply_options()


async def async_unload_entry(hass: HomeAssistant, entry: LightBuddyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(
    hass: HomeAssistant, entry: LightBuddyConfigEntry
) -> bool:
    """Migrate an older config entry."""
    if entry.version == 1 and entry.minor_version < 2:
        _async_rename_offset_entities(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


def _async_rename_offset_entities(
    hass: HomeAssistant, entry: LightBuddyConfigEntry
) -> None:
    """Give offset settings English entity ids, based on their light's.

    They got their entity ids from their name in the language of Home
    Assistant before. Entity ids already taken are left alone.
    """
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        for cls in offset_entity_classes():
            if registry_entry.domain != cls.PLATFORM or not (
                registry_entry.unique_id.endswith(cls.UNIQUE_ID_SUFFIX)
            ):
                continue
            light_entity_id = er.async_resolve_entity_id(
                registry, registry_entry.unique_id.removesuffix(cls.UNIQUE_ID_SUFFIX)
            )
            if light_entity_id is None:
                continue
            new_entity_id = cls.entity_id_for(light_entity_id)
            if new_entity_id != registry_entry.entity_id and not (
                registry.async_is_registered(new_entity_id)
            ):
                _LOGGER.debug(
                    "Renaming %s to %s", registry_entry.entity_id, new_entity_id
                )
                registry.async_update_entity(
                    registry_entry.entity_id, new_entity_id=new_entity_id
                )
