"""A color temperature offset slider per light."""

from typing import override

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import LightOffsetEntity
from .manager import LightBuddyConfigEntry
from .offset import OFFSET_LIMITS, OFFSET_STEPS, UNIT_SYMBOLS

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the offset sliders.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.NUMBER] = async_add_entities


class ColorTempOffsetNumber(LightOffsetEntity, NumberEntity):
    """Shift the color temperature a room light sends to one light."""

    PLATFORM = Platform.NUMBER
    UNIQUE_ID_SUFFIX = "_color_temp_offset"
    TRANSLATION_KEY = "color_temp_offset"
    ENTITY_ID_SUFFIX = "color_temperature_offset"

    _attr_mode = NumberMode.SLIDER

    @property
    @override
    def native_value(self) -> float:
        """Return the offset in its unit."""
        return self.offset.value

    @property
    @override
    def native_unit_of_measurement(self) -> str:
        """Return the unit chosen for the offset."""
        return UNIT_SYMBOLS[self.offset.unit]

    @property
    @override
    def native_min_value(self) -> float:
        """Return the largest offset towards warm or cold."""
        return -OFFSET_LIMITS[self.offset.unit]

    @property
    @override
    def native_max_value(self) -> float:
        """Return the largest offset towards cold or warm."""
        return OFFSET_LIMITS[self.offset.unit]

    @property
    @override
    def native_step(self) -> float:
        """Return the slider step for the unit."""
        return OFFSET_STEPS[self.offset.unit]

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Store the offset and show it on the light right away."""
        await self._manager.async_set_offset(
            self.registry_id, self.offset.with_value(value)
        )
