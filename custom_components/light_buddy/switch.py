"""A schedule switch and a disco switch per room."""

from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoomEntity
from .manager import LightBuddyConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the room switches.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.SWITCH] = async_add_entities


class ScheduleSwitch(RoomEntity, SwitchEntity):
    """Let the room follow its schedule.

    A color set by hand turns it off until the room is off, the automatic
    resume, or turning it on again.
    """

    PLATFORM = Platform.SWITCH
    UNIQUE_ID_SUFFIX = "_schedule"
    TRANSLATION_KEY = "schedule"

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the room follows its schedule right now."""
        return self._automation.enabled and not self._automation.paused

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Follow the schedule again, and apply it right away.

        Already following it, nothing changes.
        """
        if not self._automation.enabled:
            await self._automation.async_set_enabled(True)
        elif self._automation.paused:
            await self._automation.async_resume()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop following the schedule. Lights keep what they show."""
        await self._automation.async_set_enabled(False)


class DiscoSwitch(RoomEntity, SwitchEntity):
    """Let the colored lights of the room change colors."""

    PLATFORM = Platform.SWITCH
    UNIQUE_ID_SUFFIX = "_disco"
    TRANSLATION_KEY = "disco"

    _attr_icon = "mdi:party-popper"

    @property
    @override
    def is_on(self) -> bool:
        """Return True while disco runs."""
        return self._automation.disco.running

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start changing colors. White lights turn off meanwhile."""
        await self._automation.disco.async_start()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop changing colors, and restore what the lights showed before."""
        await self._automation.disco.async_stop(restore=True)
