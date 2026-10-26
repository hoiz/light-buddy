"""The schedule's target per room."""

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import Platform, UnitOfTemperature
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
    """Set up the room sensors.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.SENSOR] = async_add_entities


class TargetColorTempSensor(RoomEntity, SensorEntity):
    """The color temperature the schedule wants in a room right now."""

    PLATFORM = Platform.SENSOR
    UNIQUE_ID_SUFFIX = "_target_color_temp"
    TRANSLATION_KEY = "target_color_temperature"

    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN

    @property
    @override
    def native_value(self) -> int | None:
        """Return the target color temperature."""
        target = self._automation.target
        return target.kelvin if target else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the target brightness in percent, if the schedule has one."""
        target = self._automation.target
        return {"brightness": target.brightness if target else None}
