"""The Light Buddy integration: one light entity per area."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .manager import LightBuddyConfigEntry, RoomManager

PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.NUMBER]


async def async_setup_entry(hass: HomeAssistant, entry: LightBuddyConfigEntry) -> bool:
    """Set up Light Buddy from a config entry."""
    entry.runtime_data = RoomManager(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.runtime_data.async_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LightBuddyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
