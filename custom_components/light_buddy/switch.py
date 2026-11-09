"""A schedule switch and a disco switch per room, and the cozy mode."""

from typing import Any, override

from homeassistant.components.light import color_supported, color_temp_supported
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoomEntity
from .manager import COZY_SWITCH_UNIQUE_ID, LightBuddyConfigEntry, RoomManager

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightBuddyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the cozy mode switch, and the room switches.

    The room manager adds the room switches once all platforms are set up.
    """
    entry.runtime_data.async_add_entities[Platform.SWITCH] = async_add_entities
    async_add_entities([CozySwitch(entry.runtime_data)])


class CozySwitch(SwitchEntity):
    """Let all rooms follow a warmer curve, until the next night.

    Rooms whose schedule is off or paused are left alone, as always.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "cozy_mode"
    _attr_unique_id = COZY_SWITCH_UNIQUE_ID
    _attr_icon = "mdi:sofa"

    def __init__(self, manager: RoomManager) -> None:
        """Initialize the switch."""
        self._manager = manager
        # Set, as Home Assistant would derive it from the translated name.
        self.entity_id = f"{Platform.SWITCH}.cozy_mode"

    @override
    async def async_added_to_hass(self) -> None:
        """Follow cozy mode turning on and off."""
        self.async_on_remove(
            self._manager.async_add_schedule_listener(self.async_write_ha_state)
        )

    @property
    @override
    def is_on(self) -> bool:
        """Return True while cozy mode is on."""
        return self._manager.cozy_since is not None

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Move all rooms to the cozy curve."""
        self._manager.async_set_cozy(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Move all rooms back to the normal curve."""
        self._manager.async_set_cozy(False)


class ScheduleSwitch(RoomEntity, SwitchEntity):
    """Let the room follow its schedule.

    A color set by hand turns it off until the room is off, the automatic
    resume, or turning it on again.
    """

    PLATFORM = Platform.SWITCH
    UNIQUE_ID_SUFFIX = "_schedule"
    TRANSLATION_KEY = "schedule"
    NEEDS = staticmethod(color_temp_supported)

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
    NEEDS = staticmethod(color_supported)

    @callback
    @override
    def async_dropped(self) -> None:
        """End a running disco, as nothing could turn it off any more."""
        disco = self._automation.disco
        if disco.running:
            self._automation.async_track(disco.async_stop(restore=True))

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
