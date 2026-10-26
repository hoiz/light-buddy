"""Base for the offset settings of a single light."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from homeassistant.const import EntityCategory, Platform
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import Entity

from .offset import ColorTempOffset

if TYPE_CHECKING:
    from .manager import RoomManager


class LightOffsetEntity(Entity):
    """A setting of one light's offset, on the light's own device.

    Configuration entities stay out of dashboards and area actions.
    """

    PLATFORM: ClassVar[Platform]
    UNIQUE_ID_SUFFIX: ClassVar[str]
    TRANSLATION_KEY: ClassVar[str]

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
        self.device_entry = device
        self._light_name = light_name
        # Lights that are not the main entity of their device, or have no
        # device, need their own name in the setting's name.
        if light_name is None:
            self._attr_translation_key = self.TRANSLATION_KEY
        else:
            self._attr_translation_key = f"{self.TRANSLATION_KEY}_light"
            self._attr_translation_placeholders = {"light": light_name}

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
