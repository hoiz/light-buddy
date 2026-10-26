"""Fixtures for Light Buddy tests."""

from collections.abc import Callable
import datetime as dt
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_buddy.automation import RoomAutomation, ScheduleSettings
from custom_components.light_buddy.const import DOMAIN
from custom_components.light_buddy.schedule import SchedulePoint
from homeassistant.components.light import ColorMode
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

COLOR_LIGHT = {
    "supported_color_modes": [ColorMode.COLOR_TEMP, ColorMode.XY],
    "min_color_temp_kelvin": 2200,
    "max_color_temp_kelvin": 4000,
    "supported_features": 40,  # flash | transition
}
WHITE_LIGHT = {
    "supported_color_modes": [ColorMode.COLOR_TEMP],
    "min_color_temp_kelvin": 2700,
    "max_color_temp_kelvin": 6500,
    "supported_features": 40,
}
PLUG = {"supported_color_modes": [ColorMode.ONOFF], "supported_features": 0}

# The schedule most tests use: clock times only, so they do not depend on the
# sun. Coldest at 12:30, 2200 K from 22:30 to 05:30.
CLOCK_SCHEDULE = tuple(
    SchedulePoint(kelvin, dt.time.fromisoformat(time))
    for time, kelvin in (
        ("05:30", 2200),
        ("06:30", 3000),
        ("08:00", 4000),
        ("12:30", 5000),
        ("16:00", 4500),
        ("18:30", 3500),
        ("21:00", 2700),
        ("22:30", 2200),
    )
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations in all tests."""


@pytest.fixture
def source_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry that owns the devices of the member lights."""
    entry = MockConfigEntry(domain="mqtt")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def add_light(hass: HomeAssistant, source_entry: MockConfigEntry) -> Callable[..., str]:
    """Register a light and give it a state."""

    def _add_light(
        object_id: str,
        *,
        area: str | None = None,
        device_area: str | None = None,
        platform: str = "mqtt",
        attributes: dict[str, Any] | None = None,
        state: str = STATE_OFF,
        **registry_kwargs: Any,
    ) -> str:
        device_id = None
        if device_area is not None:
            device = dr.async_get(hass).async_get_or_create(
                config_entry_id=source_entry.entry_id,
                identifiers={("mqtt", object_id)},
                name=object_id,
            )
            dr.async_get(hass).async_update_device(
                device.id, area_id=_area_id(hass, device_area)
            )
            device_id = device.id
        entry = er.async_get(hass).async_get_or_create(
            "light",
            platform,
            object_id,
            suggested_object_id=object_id,
            device_id=device_id,
            **registry_kwargs,
        )
        if area is not None:
            er.async_get(hass).async_update_entity(
                entry.entity_id, area_id=_area_id(hass, area)
            )
        hass.states.async_set(entry.entity_id, state, attributes or COLOR_LIGHT)
        return entry.entity_id

    return _add_light


def _area_id(hass: HomeAssistant, name: str) -> str:
    registry = ar.async_get(hass)
    area = registry.async_get_area_by_name(name) or registry.async_create(name)
    return area.id


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Any]:
    """Set up Light Buddy, optionally with options."""

    async def _setup(
        options: dict[str, Any] | None = None,
        *,
        schedule: bool = False,
        sun: bool = False,
    ) -> MockConfigEntry:
        """Set up the integration.

        Schedules do not start unless asked for, so tests of other features
        are not affected by the time of day. They follow a clock time curve
        unless the sun is asked for.
        """
        if not schedule:
            # Without a target, the schedule does nothing.
            monkeypatch.setattr(RoomAutomation, "async_start", lambda self: None)
            monkeypatch.setattr(
                RoomAutomation, "_async_update_target", lambda self: None
            )
        if not sun:
            monkeypatch.setattr(
                ScheduleSettings, "points", property(lambda self: CLOCK_SCHEDULE)
            )
        entry = MockConfigEntry(
            domain=DOMAIN, title="Light Buddy", options=options or {}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup
