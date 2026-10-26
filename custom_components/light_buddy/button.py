"""A button per room to resume the schedule after manual changes."""

from typing import override

from homeassistant.components.button import ButtonEntity
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
    """Set up the room buttons.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.BUTTON] = async_add_entities


class ResumeScheduleButton(RoomEntity, ButtonEntity):
    """Let the room follow its schedule again after manual changes."""

    PLATFORM = Platform.BUTTON
    UNIQUE_ID_SUFFIX = "_resume_schedule"
    TRANSLATION_KEY = "resume_schedule"

    @override
    async def async_press(self) -> None:
        """End the pause and apply the schedule right away."""
        await self._automation.async_resume()
