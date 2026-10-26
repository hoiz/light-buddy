"""Tests for sending commands to the lights of a room."""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components.light import DATA_COMPONENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util


async def _setup(hass: HomeAssistant, add_light, setup_integration, mode: str):
    ikea = add_light("ikea", device_area="Wohnzimmer")
    other = add_light("other", device_area="Wohnzimmer")
    device_id = er.async_get(hass).async_get(ikea).device_id
    dr.async_get(hass).async_update_device(device_id, manufacturer="IKEA of Sweden")
    await setup_integration({"settings": {"separate_commands": mode}})
    return ikea, other


async def test_separate_commands_for_ikea(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """IKEA lights get the color first and the brightness after a pause."""
    ikea, other = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")

    await room.async_turn_on(brightness=100, color_temp_kelvin=3000, transition=1)
    assert [call.data for call in calls] == [
        {
            "entity_id": [other],
            "brightness": 100,
            "color_temp_kelvin": 3000,
            "transition": 1,
        },
        {"entity_id": [ikea], "color_temp_kelvin": 3000, "transition": 1},
    ]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert calls[2].data == {"entity_id": [ikea], "brightness": 100, "transition": 1}
    sent = room._commander.last_sent[ikea]
    assert sent["brightness"].sent_at > sent["color_temp_kelvin"].sent_at


async def test_separate_commands_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Without the setting, every light gets one command."""
    ikea, other = await _setup(hass, add_light, setup_integration, "off")
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")

    await room.async_turn_on(brightness=100, color_temp_kelvin=3000)
    assert [call.data for call in calls] == [
        {"entity_id": [ikea, other], "brightness": 100, "color_temp_kelvin": 3000}
    ]


async def test_separate_commands_all(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """With all lights, only commands with brightness and color are split."""
    ikea, other = await _setup(hass, add_light, setup_integration, "all")
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")

    await room.async_turn_on(brightness=100)
    assert [call.data for call in calls] == [
        {"entity_id": [ikea, other], "brightness": 100}
    ]
