"""Base classes for the entities next to the room lights."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from homeassistant.const import EntityCategory, Platform
from homeassistant.core import split_entity_id
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import Entity

from .offset import ColorTempOffset

if TYPE_CHECKING:
    from .automation import RoomAutomation
    from .manager import RoomManager


class LightOffsetEntity(Entity):
    """A setting of one light's offset, on the light's own device.

    Configuration entities stay out of dashboards and area actions.
    """

    PLATFORM: ClassVar[Platform]
    UNIQUE_ID_SUFFIX: ClassVar[str]
    TRANSLATION_KEY: ClassVar[str]
    ENTITY_ID_SUFFIX: ClassVar[str]

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        manager: RoomManager,
        light_entry: er.RegistryEntry,
        device: dr.DeviceEntry | None,
        light_name: str | None,
    ) -> None:
        """Initialize the setting."""
        self._manager = manager
        self.registry_id = light_entry.id
        self._attr_unique_id = f"{light_entry.id}{self.UNIQUE_ID_SUFFIX}"
        self.entity_id = self.entity_id_for(light_entry.entity_id)
        self.device_entry = device
        self._light_name = light_name
        # Lights that are not the main entity of their device, or have no
        # device, need their own name in the setting's name.
        if light_name is None:
            self._attr_translation_key = self.TRANSLATION_KEY
        else:
            self._attr_translation_key = f"{self.TRANSLATION_KEY}_light"
            self._attr_translation_placeholders = {"light": light_name}

    @classmethod
    def entity_id_for(cls, light_entity_id: str) -> str:
        """Return the entity id for a light's setting.

        Set from the light's entity id, as Home Assistant would otherwise
        derive it from the name in its own language.
        """
        return f"{cls.PLATFORM}.{split_entity_id(light_entity_id)[1]}_{cls.ENTITY_ID_SUFFIX}"

    @property
    def offset(self) -> ColorTempOffset:
        """Return the light's offset."""
        return self._manager.offset(self.registry_id)

    def matches(self, device: dr.DeviceEntry | None, light_name: str | None) -> bool:
        """Return True if the setting still belongs to this device and name."""
        # Home Assistant itself follows a renamed device.
        device_id = device.id if device else None
        own_device_id = self.device_entry.id if self.device_entry else None
        return device_id == own_device_id and light_name == self._light_name


class RoomEntity(Entity):
    """An entity of a room, named after its area, without a device.

    Like the room light, it is not assigned to the area, so area actions do
    not reach it.
    """

    PLATFORM: ClassVar[Platform]
    UNIQUE_ID_SUFFIX: ClassVar[str]
    TRANSLATION_KEY: ClassVar[str]

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, automation: RoomAutomation, area_name: str) -> None:
        """Initialize the entity."""
        self._automation = automation
        self.area_name = area_name
        area_id = automation.area_id
        self.entity_id = f"{self.PLATFORM}.{self.TRANSLATION_KEY}_{area_id}"
        self._attr_unique_id = f"{area_id}{self.UNIQUE_ID_SUFFIX}"
        self._attr_translation_key = self.TRANSLATION_KEY
        self._attr_translation_placeholders = {"area": area_name}

    async def async_added_to_hass(self) -> None:
        """Follow changes of the room's automation."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._automation.async_add_listener(self.async_write_ha_state)
        )
