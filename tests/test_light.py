"""Tests for the room light entity."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_buddy.const import DOMAIN
from homeassistant.components.light import DATA_COMPONENT, ColorMode, LightEntity
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT, PLUG, WHITE_LIGHT

ROOM = "light.all_wohnzimmer"


def _room(hass: HomeAssistant, entity_id: str = ROOM) -> LightEntity:
    """Return the room light entity.

    Tests call it directly, because the mocked light services would also
    replace the service call to the room light.
    """
    return hass.data[DATA_COMPONENT].get_entity(entity_id)


def _registry_id(hass: HomeAssistant, entity_id: str) -> str:
    """Return the entity registry id, which offsets are stored by."""
    return er.async_get(hass).async_get(entity_id).id


async def _settle(hass: HomeAssistant) -> None:
    """Let the membership debouncer run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()


async def test_state_follows_members(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """The room is on if any light is on and averages the on lights."""
    color = add_light("color", area="Wohnzimmer")
    white = add_light("white", area="Wohnzimmer", attributes=WHITE_LIGHT)
    add_light("plug", area="Wohnzimmer", attributes=PLUG)
    await setup_integration()

    state = hass.states.get(ROOM)
    assert state.state == STATE_OFF
    assert state.name == "All lights in Wohnzimmer"
    assert set(state.attributes["supported_color_modes"]) == {
        ColorMode.HS,
        ColorMode.COLOR_TEMP,
    }
    assert state.attributes["min_color_temp_kelvin"] == 2200
    assert state.attributes["max_color_temp_kelvin"] == 6500

    on = {"color_mode": ColorMode.COLOR_TEMP}
    hass.states.async_set(
        color,
        STATE_ON,
        {**COLOR_LIGHT, **on, "brightness": 100, "color_temp_kelvin": 2600},
    )
    hass.states.async_set(
        white,
        STATE_ON,
        {**WHITE_LIGHT, **on, "brightness": 200, "color_temp_kelvin": 3000},
    )
    await hass.async_block_till_done()

    state = hass.states.get(ROOM)
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 150
    assert state.attributes["color_temp_kelvin"] == 2800
    assert state.attributes["color_mode"] == ColorMode.COLOR_TEMP

    hass.states.async_set(
        color,
        STATE_ON,
        {
            **COLOR_LIGHT,
            "color_mode": ColorMode.XY,
            "brightness": 100,
            "hs_color": (240, 100),
        },
    )
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    await hass.async_block_till_done()
    state = hass.states.get(ROOM)
    assert state.attributes["color_mode"] == ColorMode.HS
    assert state.attributes["hs_color"] == (240, 100)
    assert state.attributes["brightness"] == 100


async def test_only_brightness_and_onoff(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Rooms without color lights offer the best mode their lights have."""
    plug = add_light("plug", area="Bad", attributes=PLUG)
    add_light(
        "dimmer",
        area="Flur",
        attributes={"supported_color_modes": [ColorMode.BRIGHTNESS]},
    )
    await setup_integration()

    assert hass.states.get("light.all_bad").attributes["supported_color_modes"] == [
        ColorMode.ONOFF
    ]
    assert hass.states.get("light.all_flur").attributes["supported_color_modes"] == [
        ColorMode.BRIGHTNESS
    ]

    hass.states.async_set(plug, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get("light.all_bad").state == STATE_UNAVAILABLE


async def test_turn_on_bundles_calls(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights with identical params share a call, colors skip white lights."""
    color_a = add_light("color_a", area="Wohnzimmer")
    color_b = add_light("color_b", area="Wohnzimmer")
    white = add_light("white", area="Wohnzimmer", attributes=WHITE_LIGHT)
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(brightness=128, hs_color=(120, 50))

    assert [call.data for call in calls] == [
        {"entity_id": [color_a, color_b], "brightness": 128, "hs_color": (120, 50)},
        {"entity_id": [white], "brightness": 128},
    ]


async def test_turn_off_skips_off_lights(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Only lights that are not off receive turn_off."""
    add_light("off", area="Wohnzimmer")
    on = add_light("on", area="Wohnzimmer", state=STATE_ON)
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_off")

    await _room(hass).async_turn_off(transition=2, flash="short")

    assert [call.data for call in calls] == [
        {"entity_id": [on], "transition": 2, "flash": "short"}
    ]


async def test_turn_off_after_pending_turn_on(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A quick turn_off wins over a turn_on, even before the lights report on."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration()
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")

    room = _room(hass)
    await room.async_turn_on(brightness=10)
    await room.async_turn_on(brightness=20)
    await room.async_turn_off()
    assert len(on_calls) == 1
    hass.states.async_set(lamp, STATE_ON, COLOR_LIGHT)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert len(on_calls) == 1
    assert [call.data for call in off_calls] == [{"entity_id": [lamp]}]


async def test_merged_commands_keep_one_color(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A newer color descriptor replaces an older pending one."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_on")

    room = _room(hass)
    await room.async_turn_on(brightness=10)
    await room.async_turn_on(brightness=20, hs_color=(120, 50))
    await room.async_turn_on(color_temp_kelvin=3000)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls[-1].data == {
        "entity_id": [lamp],
        "brightness": 20,
        "color_temp_kelvin": 3000,
    }


async def test_fast_commands_are_merged(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Commands within the interval are merged, the latest value wins."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_on")

    room = _room(hass)
    for brightness in (10, 20, 30):
        await room.async_turn_on(brightness=brightness)
    await room.async_turn_on(color_temp_kelvin=3000)
    assert [call.data for call in calls] == [{"entity_id": [lamp], "brightness": 10}]

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert [call.data for call in calls][1:] == [
        {"entity_id": [lamp], "brightness": 30, "color_temp_kelvin": 3000}
    ]


async def test_membership_follows_registry(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Moved lights, new areas and renamed areas apply without a restart."""
    lamp = add_light("lamp", area="Wohnzimmer")
    other = add_light("other", area="Wohnzimmer")
    await setup_integration()
    assert hass.states.get(ROOM).attributes["entity_id"] == [lamp, other]

    kitchen = ar.async_get(hass).async_create("Küche")
    er.async_get(hass).async_update_entity(lamp, area_id=kitchen.id)
    await _settle(hass)
    assert hass.states.get(ROOM).attributes["entity_id"] == [other]
    assert hass.states.get("light.all_kuche").attributes["entity_id"] == [lamp]

    # Membership changes also change which states the room follows.
    hass.states.async_set(lamp, STATE_ON, COLOR_LIGHT)
    await hass.async_block_till_done()
    assert hass.states.get("light.all_kuche").state == STATE_ON
    assert hass.states.get(ROOM).state == STATE_OFF

    ar.async_get(hass).async_update(kitchen.id, name="Kochnische")
    await _settle(hass)
    assert hass.states.get("light.all_kuche").name == "All lights in Kochnische"

    er.async_get(hass).async_update_entity(lamp, area_id=None)
    await _settle(hass)
    assert hass.states.get("light.all_kuche") is None
    assert er.async_get(hass).async_get_entity_id("light", DOMAIN, kitchen.id) is None


async def test_room_lights_do_not_include_each_other(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Moving a room light into an area does not make it a member."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration()

    er.async_get(hass).async_update_entity(ROOM, area_id="wohnzimmer")
    await _settle(hass)
    assert hass.states.get(ROOM).attributes["entity_id"] == [lamp]


async def test_changed_entity_id_keeps_following_members(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After the user changes the entity id, membership updates still apply."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration()

    er.async_get(hass).async_update_entity(ROOM, new_entity_id="light.wohnzimmer_all")
    await _settle(hass)
    other = add_light("other", area="Wohnzimmer")
    await _settle(hass)

    state = hass.states.get("light.wohnzimmer_all")
    assert state.attributes["entity_id"] == [lamp, other]
    assert hass.states.get(ROOM) is None


async def test_rename_area_of_disabled_room_light(
    hass: HomeAssistant, add_light, setup_integration, caplog
) -> None:
    """Renaming the area of a disabled room light does not fail."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration()
    er.async_get(hass).async_update_entity(
        ROOM, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ROOM) is None

    ar.async_get(hass).async_update("wohnzimmer", name="Stube")
    await _settle(hass)
    assert "Traceback" not in caplog.text
    assert hass.states.get(ROOM) is None


async def test_offset_applies_and_is_hidden(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Offsets shift the sent color temperature, the room shows the requested one."""
    plain = add_light("plain", area="Wohnzimmer", attributes=COLOR_LIGHT)
    cold = add_light("cold", area="Wohnzimmer")
    clamped = add_light("clamped", area="Wohnzimmer", attributes=COLOR_LIGHT)
    await setup_integration(
        {
            "offsets": {
                _registry_id(hass, cold): {"value": -300, "unit": "K"},
                _registry_id(hass, clamped): {"value": -500, "unit": "K"},
            }
        }
    )
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(color_temp_kelvin=2600, brightness=100)
    assert [call.data for call in calls] == [
        # 2100 K is below the light's minimum of 2200 K.
        {"entity_id": [clamped], "color_temp_kelvin": 2200, "brightness": 100},
        {"entity_id": [cold], "color_temp_kelvin": 2300, "brightness": 100},
        {"entity_id": [plain], "color_temp_kelvin": 2600, "brightness": 100},
    ]

    on = {"color_mode": ColorMode.COLOR_TEMP, "brightness": 100}
    hass.states.async_set(
        plain, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": 2600}
    )
    hass.states.async_set(
        cold, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": 2304}
    )
    hass.states.async_set(
        clamped, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": 2200}
    )
    await hass.async_block_till_done()
    assert hass.states.get(ROOM).attributes["color_temp_kelvin"] == 2600

    # Changed elsewhere: the offset is taken out of the reported value.
    hass.states.async_set(
        cold, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": 3700}
    )
    hass.states.async_set(plain, STATE_OFF, COLOR_LIGHT)
    hass.states.async_set(clamped, STATE_OFF, COLOR_LIGHT)
    await hass.async_block_till_done()
    assert hass.states.get(ROOM).attributes["color_temp_kelvin"] == 4000


async def test_offset_survives_entity_id_change(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Offsets follow a light when its entity id changes."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration(
        {"offsets": {_registry_id(hass, lamp): {"value": -300, "unit": "K"}}}
    )
    er.async_get(hass).async_update_entity(lamp, new_entity_id="light.stehlampe")
    hass.states.async_set("light.stehlampe", STATE_OFF, COLOR_LIGHT)
    await _settle(hass)
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(color_temp_kelvin=3000)
    assert [call.data for call in calls] == [
        {"entity_id": ["light.stehlampe"], "color_temp_kelvin": 2700}
    ]


async def test_failing_call_does_not_block_others(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """When one call fails, the other lights still get their command."""
    first = add_light("a_first", area="Wohnzimmer")
    second = add_light("b_second", area="Wohnzimmer")
    await setup_integration(
        {"offsets": {_registry_id(hass, first): {"value": -300, "unit": "K"}}}
    )
    calls = []

    async def turn_on(call) -> None:
        calls.append(call.data)
        if first in call.data["entity_id"]:
            raise HomeAssistantError("unreachable")

    hass.services.async_register("light", "turn_on", turn_on)

    with pytest.raises(HomeAssistantError):
        await _room(hass).async_turn_on(color_temp_kelvin=3000)
    assert [data["entity_id"] for data in calls] == [[first], [second]]
    # Only what was really sent is remembered.
    assert set(_room(hass)._commander.sent_color_temps) == {second}


async def test_shown_color_temp_follows_other_changes(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light changed elsewhere shows its own value, within the room's range."""
    cold = add_light("cold", area="Wohnzimmer")
    warm = add_light("warm", area="Wohnzimmer")
    await setup_integration(
        {
            "offsets": {
                _registry_id(hass, cold): {"value": 300, "unit": "K"},
                _registry_id(hass, warm): {"value": -300, "unit": "K"},
            }
        }
    )
    async_mock_service(hass, "light", "turn_on")
    await _room(hass).async_turn_on(color_temp_kelvin=3900)
    on = {"color_mode": ColorMode.COLOR_TEMP, "brightness": 100}

    # 4200 K was clamped to the maximum of 4000 K, the room shows the request.
    hass.states.async_set(
        cold, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": 4000}
    )
    await hass.async_block_till_done()
    assert hass.states.get(ROOM).attributes["color_temp_kelvin"] == 3900

    # Changed elsewhere: the offset is taken out of the reported value, also
    # once the light is back at the value that was sent.
    for reported, shown in ((3000, 2700), (4000, 3700)):
        hass.states.async_set(
            cold, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": reported}
        )
        await hass.async_block_till_done()
        assert hass.states.get(ROOM).attributes["color_temp_kelvin"] == shown

    # 3900 K plus 300 K is beyond the room's maximum of 4000 K.
    hass.states.async_set(cold, STATE_OFF, COLOR_LIGHT)
    hass.states.async_set(
        warm, STATE_ON, {**COLOR_LIGHT, **on, "color_temp_kelvin": 3900}
    )
    await hass.async_block_till_done()
    assert hass.states.get(ROOM).attributes["color_temp_kelvin"] == 4000
