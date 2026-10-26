"""The unit of a light's color temperature offset."""

from typing import override

from homeassistant.components.select import SelectEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import UNIT_KELVIN, UNIT_MIRED
from .entity import LightOffsetEntity
from .manager import LightBuddyConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the offset unit selects.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.SELECT] = async_add_entities


class ColorTempOffsetUnitSelect(LightOffsetEntity, SelectEntity):
    """Choose whether a light's offset is in Kelvin or mired."""

    PLATFORM = Platform.SELECT
    UNIQUE_ID_SUFFIX = "_color_temp_offset_unit"
    TRANSLATION_KEY = "color_temp_offset_unit"
    ENTITY_ID_SUFFIX = "color_temperature_offset_unit"

    _attr_options = [UNIT_KELVIN, UNIT_MIRED]

    @property
    @override
    def current_option(self) -> str:
        """Return the unit of the offset."""
        return self.offset.unit

    @override
    async def async_select_option(self, option: str) -> None:
        """Convert the offset, so the light looks the same at 3000 K."""
        await self._manager.async_set_offset(
            self.registry_id, self.offset.converted(option)
        )
