"""A color temperature offset slider per light."""

from typing import override

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .manager import OFFSET_SUFFIX, LightBuddyConfigEntry, RoomManager

PARALLEL_UPDATES = 0

# Larger shifts leave the range of common lights.
MAX_OFFSET = 1500
OFFSET_STEP = 50


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the offset sliders.

    The room manager adds them once all platforms are set up.
    """
    entry.runtime_data.async_add_numbers = async_add_entities


class ColorTempOffsetNumber(NumberEntity):
    """Shift the color temperature a room light sends to one light.

    The slider sits on the light's device, among its configuration entities,
    so it is out of dashboards and area actions.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_max_value = MAX_OFFSET
    _attr_native_min_value = -MAX_OFFSET
    _attr_native_step = OFFSET_STEP
    _attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
    _attr_should_poll = False

    def __init__(
        self,
        manager: RoomManager,
        light_entry: er.RegistryEntry,
        device: dr.DeviceEntry | None,
        light_name: str | None,
    ) -> None:
        """Initialize the slider."""
        self._manager = manager
        self._light_entry = light_entry
        self._attr_unique_id = f"{light_entry.id}{OFFSET_SUFFIX}"
        self.device_entry = device
        self._light_name = light_name
        if light_name is None:
            self._attr_translation_key = "color_temp_offset"
        else:
            self._attr_translation_key = "color_temp_offset_light"
            self._attr_translation_placeholders = {"light": light_name}

    def matches(self, device: dr.DeviceEntry | None, light_name: str | None) -> bool:
        """Return True if the slider still belongs to this device and name."""
        # Home Assistant itself follows a renamed device.
        device_id = device.id if device else None
        own_device_id = self.device_entry.id if self.device_entry else None
        return device_id == own_device_id and light_name == self._light_name

    @property
    @override
    def native_value(self) -> float:
        """Return the offset in Kelvin."""
        return self._manager.offset(self._light_entry.id)

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Store the offset and show it on the light right away."""
        await self._manager.async_set_offset(self._light_entry.id, round(value))
        self.async_write_ha_state()
