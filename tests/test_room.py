"""Tests for sending commands to the lights of a room."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components.light import DATA_COMPONENT
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT


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
        # Each command gets half of the transition.
        {"entity_id": [ikea], "color_temp_kelvin": 3000, "transition": 0.5},
    ]

    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert calls[2].data == {
        "entity_id": [ikea],
        "brightness": 100,
        "transition": 0.5,
    }
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


async def test_turn_off_cancels_waiting_brightness(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning off right after turning on keeps the lights off."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    on_calls = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")

    await room.async_turn_on(brightness=100, color_temp_kelvin=3000)
    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT)
    await room._commander._async_send("turn_off", {ikea: {}}, None)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert all("brightness" not in call.data for call in on_calls[1:])


async def test_waiting_brightness_skips_lights_turned_off(
    hass: HomeAssistant, add_light, setup_integration, freezer
) -> None:
    """A light switched off after its color, e.g. by a wall switch, stays off."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")

    await room.async_turn_on(brightness=100, color_temp_kelvin=3000)
    freezer.tick(0.1)
    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT)
    hass.states.async_set(ikea, STATE_OFF, COLOR_LIGHT)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_waiting_brightness_for_lights_not_reported_on(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light that did not report on yet still gets its brightness."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")

    await room.async_turn_on(brightness=100, color_temp_kelvin=3000)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": [ikea], "brightness": 100}


async def test_color_only_command_keeps_waiting_brightness(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Only a newer brightness replaces one still waiting."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    commander = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")._commander

    await commander.async_send_now(
        {ikea: {"brightness": 200, "color_temp_kelvin": 3000}}
    )
    await commander.async_send_now({ikea: {"color_temp_kelvin": 3100}})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": [ikea], "brightness": 200}


async def test_newer_command_replaces_waiting_brightness(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Brightness still waiting is dropped when the lights get a newer command."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")
    commander = room._commander

    await commander.async_send_now(
        {ikea: {"brightness": 200, "color_temp_kelvin": 3000, "transition": 10}}
    )
    await commander.async_send_now({ikea: {"brightness": 50}})
    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert [call.data.get("brightness") for call in calls] == [None, 50]


async def test_own_turn_off_report_keeps_waiting_brightness(
    hass: HomeAssistant, add_light, setup_integration, freezer
) -> None:
    """A late off report of our own earlier turn off is no wall switch."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    room = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")
    commander = room._commander
    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT)
    await commander._async_send("turn_off", {ikea: {}}, None)
    off_context = commander._own_context_ids[-1]

    await commander.async_send_now(
        {ikea: {"brightness": 100, "color_temp_kelvin": 3000}}
    )
    freezer.tick(0.1)
    hass.states.async_set(ikea, STATE_OFF, COLOR_LIGHT, context=Context(id=off_context))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": [ikea], "brightness": 100}


async def test_no_waiting_task_without_lights(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """When every color fails, nothing waits to send a brightness."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")

    async def fail(call) -> None:
        raise HomeAssistantError("unreachable")

    hass.services.async_register("light", "turn_on", fail)
    commander = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")._commander
    with pytest.raises(HomeAssistantError):
        await commander.async_send_now(
            {ikea: {"brightness": 100, "color_temp_kelvin": 3000}}
        )
    assert commander._later == {}


async def test_wall_switch_in_color_context_stays_off(
    hass: HomeAssistant, add_light, setup_integration, freezer
) -> None:
    """Lights keep the color's context a while, which is no turn off of ours."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    commander = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")._commander

    await commander.async_send_now(
        {ikea: {"brightness": 100, "color_temp_kelvin": 3000}}
    )
    color_context = calls[0].context
    freezer.tick(0.1)
    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT, context=color_context)
    hass.states.async_set(ikea, STATE_OFF, COLOR_LIGHT, context=color_context)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_late_off_of_own_turn_off_keeps_brightness(
    hass: HomeAssistant, add_light, setup_integration, freezer
) -> None:
    """Off reported late for our own turn off, in the color's context, is ours."""
    ikea, _ = await _setup(hass, add_light, setup_integration, "ikea")
    calls = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    commander = hass.data[DATA_COMPONENT].get_entity("light.all_wohnzimmer")._commander
    hass.states.async_set(ikea, STATE_ON, COLOR_LIGHT)
    await commander._async_send("turn_off", {ikea: {"transition": 1}}, None)

    await commander.async_send_now(
        {ikea: {"brightness": 100, "color_temp_kelvin": 3000}}
    )
    color_context = calls[0].context
    freezer.tick(0.1)
    hass.states.async_set(ikea, STATE_OFF, COLOR_LIGHT, context=color_context)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": [ikea], "brightness": 100}
