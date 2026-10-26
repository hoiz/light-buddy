"""The schedule's target, the same for all rooms."""

import datetime as dt
from typing import override

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import SCHEDULE_INTERVAL
from .manager import TARGET_SENSOR_UNIQUE_ID, LightBuddyConfigEntry, RoomManager
from .schedule import target_at

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the target sensor."""
    async_add_entities([TargetColorTempSensor(entry.runtime_data)])


class TargetColorTempSensor(SensorEntity):
    """The color temperature the schedule wants right now.

    Whether a room follows it shows its schedule switch.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "target_color_temperature"
    _attr_unique_id = TARGET_SENSOR_UNIQUE_ID
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN

    def __init__(self, manager: RoomManager) -> None:
        """Initialize the sensor."""
        self._manager = manager
        # Set, as Home Assistant would derive it from the translated name.
        self.entity_id = f"{Platform.SENSOR}.target_color_temperature"

    @override
    async def async_added_to_hass(self) -> None:
        """Follow the time and changes of the schedule."""
        self._async_update()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_update_later, SCHEDULE_INTERVAL
            )
        )
        self.async_on_remove(
            self._manager.async_add_schedule_listener(self._async_update_and_write)
        )

    @callback
    def _async_update(self) -> None:
        target = target_at(
            self._manager.points,
            dt_util.now(),
            lambda event, day: get_astral_event_date(self.hass, event, day),
        )
        self._attr_native_value = target.kelvin if target else None

    @callback
    def _async_update_later(self, _now: dt.datetime) -> None:
        self._async_update_and_write()

    @callback
    def _async_update_and_write(self) -> None:
        value = self._attr_native_value
        self._async_update()
        if self._attr_native_value != value:
            self.async_write_ha_state()
