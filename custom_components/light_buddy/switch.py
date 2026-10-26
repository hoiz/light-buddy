"""A schedule switch per room."""

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
    """Let the room follow its schedule."""

    PLATFORM = Platform.SWITCH
    UNIQUE_ID_SUFFIX = "_schedule"
    TRANSLATION_KEY = "schedule"

    @property
    @override
    def is_on(self) -> bool:
        """Return True if the room follows its schedule."""
        return self._automation.enabled

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Follow the schedule, and apply it right away."""
        await self._automation.async_set_enabled(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop following the schedule. Lights keep what they show."""
        await self._automation.async_set_enabled(False)
