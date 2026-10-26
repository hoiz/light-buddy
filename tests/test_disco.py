"""Tests for the disco mode."""

import asyncio
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_buddy.disco import PALETTES, step_colors
from homeassistant.components.light import DATA_COMPONENT, ColorMode
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT, WHITE_LIGHT

ROOM = "light.all_wohnzimmer"
DISCO = "switch.disco_wohnzimmer"
SCHEDULE = "switch.schedule_wohnzimmer"
RAINBOW = PALETTES["rainbow"]
# Coldest point of the default schedule, 12:30 in the test time zone.
NOON = "2026-10-26 12:30:00-07:00"


def _entity(data) -> str:
    return data["entity_id"][0]


def _ct(attributes: dict, kelvin: int, brightness: int = 100) -> dict:
    return {
        **attributes,
        "color_mode": ColorMode.COLOR_TEMP,
        "color_temp_kelvin": kelvin,
        "brightness": brightness,
    }


def _hs(hs_color: tuple[float, float], brightness: int = 100) -> dict:
    return {
        **COLOR_LIGHT,
        "color_mode": ColorMode.HS,
        "hs_color": hs_color,
        "brightness": brightness,
    }


async def _switch(hass: HomeAssistant, service: str) -> None:
    await _call_switch(hass, service)
    await hass.async_block_till_done()


async def _call_switch(hass: HomeAssistant, service: str) -> None:
    """Call the switch, without waiting for everything else to finish.

    For calls run as tasks next to each other.
    """
    await hass.services.async_call(
        "switch", service, {"entity_id": DISCO}, blocking=True
    )


def _room(hass: HomeAssistant):
    return hass.data[DATA_COMPONENT].get_entity(ROOM)


async def _later(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def test_step_colors() -> None:
    """Neighbors differ, the palette moves on, and repeats for many lights."""
    palette = ((0, 100), (120, 100), (240, 100))
    assert step_colors(["a", "b", "c", "d"], palette, 0) == {
        "a": (0, 100),
        "b": (120, 100),
        "c": (240, 100),
        "d": (0, 100),
    }
    assert step_colors(["a", "b"], palette, 2) == {"a": (240, 100), "b": (0, 100)}


async def test_start(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Colored lights get their own colors, white lights turn off."""
    a = add_light(
        "a", area="Wohnzimmer", state=STATE_ON, attributes=_ct(COLOR_LIGHT, 3000)
    )
    b = add_light("b", area="Wohnzimmer")
    c = add_light("c", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    white = add_light(
        "white", area="Wohnzimmer", state=STATE_ON, attributes=_ct(WHITE_LIGHT, 3000)
    )
    await setup_integration()
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")

    await _switch(hass, "turn_on")

    assert hass.states.get(DISCO).state == STATE_ON
    assert [call.data for call in on_calls] == [
        {"entity_id": [a], "hs_color": RAINBOW[0]},
        # Off, it turns on with the room's brightness.
        {"entity_id": [b], "hs_color": RAINBOW[1], "brightness": 100},
        {"entity_id": [c], "hs_color": RAINBOW[2]},
    ]
    assert [call.data for call in off_calls] == [{"entity_id": [white]}]
    # Our own colors are no manual change.
    assert hass.states.get(SCHEDULE).state == STATE_ON


async def test_start_in_room_that_is_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights turn on at full brightness when the room is off."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_on")

    await _switch(hass, "turn_on")

    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "hs_color": RAINBOW[0], "brightness": 255}
    ]


async def test_steps(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Colors move on with a short fade, lights turned off stay off."""
    a = add_light("a", area="Wohnzimmer", state=STATE_ON)
    b = add_light("b", area="Wohnzimmer", state=STATE_ON)
    c = add_light("c", area="Wohnzimmer", state=STATE_ON)
    await setup_integration()
    await _switch(hass, "turn_on")
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(b, STATE_OFF, COLOR_LIGHT)

    await _later(hass, 3)
    assert calls == []
    await _later(hass, 4)

    assert [call.data for call in calls] == [
        {"entity_id": [a], "hs_color": RAINBOW[1], "transition": 1},
        {"entity_id": [c], "hs_color": RAINBOW[3], "transition": 1},
    ]


async def test_lights_with_the_same_color_share_a_call(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """With more lights than colors, colors repeat in one call."""
    lights = [
        add_light(f"lamp_{index}", area="Wohnzimmer", state=STATE_ON)
        for index in range(len(RAINBOW) + 1)
    ]
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_on")

    await _switch(hass, "turn_on")

    assert calls[0].data == {
        "entity_id": [lights[0], lights[-1]],
        "hs_color": RAINBOW[0],
    }
    assert len(calls) == len(RAINBOW)


async def test_stop_restores(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Turning disco off brings back what the lights showed."""
    ct_lamp = add_light(
        "ct_lamp", area="Wohnzimmer", state=STATE_ON, attributes=_ct(COLOR_LIGHT, 3000)
    )
    hs_lamp = add_light(
        "hs_lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((120, 50), 50)
    )
    off_lamp = add_light("off_lamp", area="Wohnzimmer")
    white = add_light(
        "white",
        area="Wohnzimmer",
        state=STATE_ON,
        attributes=_ct(WHITE_LIGHT, 2700, 200),
    )
    await setup_integration()
    await _switch(hass, "turn_on")
    for lamp in (ct_lamp, hs_lamp, off_lamp):
        hass.states.async_set(lamp, STATE_ON, _hs((0, 100)))
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    await hass.async_block_till_done()
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")

    await _switch(hass, "turn_off")

    assert hass.states.get(DISCO).state == STATE_OFF
    assert sorted((call.data for call in on_calls), key=_entity) == sorted(
        [
            {"entity_id": [ct_lamp], "brightness": 100, "color_temp_kelvin": 3000},
            {"entity_id": [hs_lamp], "brightness": 50, "hs_color": (120, 50)},
            {"entity_id": [white], "brightness": 200, "color_temp_kelvin": 2700},
        ],
        key=_entity,
    )
    assert [call.data for call in off_calls] == [{"entity_id": [off_lamp]}]

    # No more steps.
    await _later(hass, 10)
    assert len(on_calls) == 3


async def test_stop_follows_schedule(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Lights following the schedule get its current target back."""
    freezer.move_to(NOON)
    limited = add_light(
        "limited", area="Wohnzimmer", state=STATE_ON, attributes=_ct(COLOR_LIGHT, 3000)
    )
    white = add_light(
        "white", area="Wohnzimmer", state=STATE_ON, attributes=_ct(WHITE_LIGHT, 3000)
    )
    await setup_integration(schedule=True)
    await _switch(hass, "turn_on")
    hass.states.async_set(limited, STATE_ON, _hs((0, 100)))
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "light", "turn_on")

    await _switch(hass, "turn_off")

    assert sorted((call.data for call in calls), key=_entity) == [
        # 5000 K is beyond the light's maximum.
        {"entity_id": [limited], "brightness": 100, "color_temp_kelvin": 4000},
        {"entity_id": [white], "brightness": 100, "color_temp_kelvin": 5000},
    ]


async def test_schedule_waits_during_disco(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The schedule sends nothing while disco runs."""
    freezer.move_to(NOON)
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    white = add_light("white", area="Wohnzimmer", attributes=WHITE_LIGHT)
    await setup_integration(schedule=True)
    await _switch(hass, "turn_on")
    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(lamp, STATE_ON, _hs((0, 100)))
    # Turned on elsewhere, it is not given the target either.
    hass.states.async_set(white, STATE_ON, _ct(WHITE_LIGHT, 3000))

    for _ in range(16):
        await _later(hass, 4)
        freezer.tick(4)

    assert calls
    assert all("color_temp_kelvin" not in call.data for call in calls)


async def test_room_off_ends_disco(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning the room off ends disco, the lights stay off."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    await setup_integration()
    await _switch(hass, "turn_on")
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")

    await _room(hass).async_turn_off()
    await hass.async_block_till_done()

    assert hass.states.get(DISCO).state == STATE_OFF
    assert [call.data for call in off_calls] == [{"entity_id": [lamp]}]
    await _later(hass, 10)
    assert on_calls == []


async def test_lights_turned_off_end_disco(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """All lights turned off elsewhere end disco, after the start."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    await setup_integration()
    await _switch(hass, "turn_on")
    calls = async_mock_service(hass, "light", "turn_on")

    # Right after the start, lights may still be switching over.
    hass.states.async_set(lamp, STATE_OFF, COLOR_LIGHT)
    await hass.async_block_till_done()
    assert hass.states.get(DISCO).state == STATE_ON

    # Lights may be slow to report on, so the first step only skips.
    freezer.tick(4)
    await _later(hass, 0)
    assert hass.states.get(DISCO).state == STATE_ON

    # The next step finds the room still off.
    freezer.tick(4)
    await _later(hass, 0)
    assert hass.states.get(DISCO).state == STATE_OFF
    assert calls == []


async def test_color_through_room_ends_disco(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A color of one's own ends disco and counts as a manual change."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    await setup_integration()
    await _switch(hass, "turn_on")
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(hs_color=(200, 80))
    await hass.async_block_till_done()

    assert hass.states.get(DISCO).state == STATE_OFF
    assert hass.states.get(SCHEDULE).state == STATE_OFF
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "hs_color": (200, 80)}
    ]
    # No more steps.
    await _later(hass, 10)
    assert len(calls) == 1


async def test_brightness_during_disco(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Brightness only reaches the colored lights, disco goes on."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    add_light("white", area="Wohnzimmer", state=STATE_ON, attributes=WHITE_LIGHT)
    await setup_integration()
    await _switch(hass, "turn_on")
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(brightness=50)
    await hass.async_block_till_done()

    assert hass.states.get(DISCO).state == STATE_ON
    assert [call.data for call in calls] == [{"entity_id": [lamp], "brightness": 50}]


async def test_settings_apply_live(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Changed settings apply to a running disco."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    entry = await setup_integration()
    await _switch(hass, "turn_on")
    calls = async_mock_service(hass, "light", "turn_on")

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "settings": {"disco_interval": 10, "disco_palette": "fire"},
        },
    )
    await hass.async_block_till_done()
    await _later(hass, 5)
    assert calls == []
    await _later(hass, 11)

    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "hs_color": PALETTES["fire"][1], "transition": 1}
    ]


async def test_no_color_lights(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Disco needs at least one light with colors."""
    add_light("white", area="Wohnzimmer", state=STATE_ON, attributes=WHITE_LIGHT)
    await setup_integration()

    with pytest.raises(HomeAssistantError) as err:
        await _switch(hass, "turn_on")
    assert err.value.translation_key == "no_color_lights"
    assert hass.states.get(DISCO).state == STATE_OFF


async def test_stop_cancels_commands_being_sent(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Colors still being sent do not land after the restore."""
    a = add_light("a", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    add_light("b", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    add_light("white", area="Wohnzimmer", state=STATE_ON, attributes=WHITE_LIGHT)
    await setup_integration()
    stuck: list[dict] = []
    release = asyncio.Event()

    async def _slow(call: ServiceCall) -> None:
        stuck.append(dict(call.data))
        await release.wait()

    hass.services.async_register("light", "turn_on", _slow)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": DISCO}, blocking=False
    )
    for _ in range(5):
        await asyncio.sleep(0)
    # The first light's color does not get through.
    assert [call["entity_id"] for call in stuck] == [[a]]

    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")
    await _switch(hass, "turn_off")
    release.set()
    await hass.async_block_till_done()
    await _later(hass, 10)

    # Only the restore: no colors and no turning off of the white light.
    assert all(
        "hs_color" not in call.data or call.data["hs_color"] == (0, 50)
        for call in on_calls
    )
    assert off_calls == []


async def test_stop_waits_for_the_fade(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """IKEA lights ignore commands while fading, so the restore waits."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    await setup_integration()
    await _switch(hass, "turn_on")
    freezer.tick(4)
    await _later(hass, 0)
    calls = async_mock_service(hass, "light", "turn_on")

    stop = hass.async_create_task(_call_switch(hass, "turn_off"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert calls == []

    freezer.tick(2)
    await _later(hass, 0)
    await stop
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "brightness": 100, "hs_color": (0, 50)}
    ]


async def test_pause_survives_the_start(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A room looking off while disco starts keeps its pause.

    Turned off, the lights then get back the color temperature set by hand,
    not the schedule's.
    """
    freezer.move_to(NOON)
    lamp = add_light("lamp", area="Wohnzimmer", attributes=COLOR_LIGHT)
    white = add_light(
        "white", area="Wohnzimmer", state=STATE_ON, attributes=_ct(WHITE_LIGHT, 2800)
    )
    await setup_integration(schedule=True)
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    await _room(hass).async_turn_on(color_temp_kelvin=2800)
    assert hass.states.get(SCHEDULE).state == STATE_OFF

    await _switch(hass, "turn_on")
    # The white light reports off before the colored one reports on.
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    await hass.async_block_till_done()
    hass.states.async_set(lamp, STATE_ON, _hs(RAINBOW[0], 255))
    await hass.async_block_till_done()
    assert hass.states.get(SCHEDULE).state == STATE_OFF

    calls = async_mock_service(hass, "light", "turn_on")
    await _switch(hass, "turn_off")
    assert [call.data for call in calls] == [
        {"entity_id": [white], "brightness": 100, "color_temp_kelvin": 2800}
    ]


async def test_lights_switched_during_disco_keep_it(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights switched by others against disco are not switched back."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    turned_off = add_light("turned_off", area="Wohnzimmer", state=STATE_ON)
    ceiling = add_light("ceiling", area="Wohnzimmer", attributes=WHITE_LIGHT)
    await setup_integration()
    await _switch(hass, "turn_on")
    hass.states.async_set(lamp, STATE_ON, _hs(RAINBOW[0]))
    hass.states.async_set(turned_off, STATE_OFF, COLOR_LIGHT)
    hass.states.async_set(ceiling, STATE_ON, _ct(WHITE_LIGHT, 3000))
    await hass.async_block_till_done()
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")

    await _switch(hass, "turn_off")

    assert [call.data["entity_id"] for call in on_calls] == [[lamp]]
    assert off_calls == []


async def _step_and_fade(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Let a step run, whose fade is still going on."""
    freezer.tick(4)
    await _later(hass, 0)


async def _settle(hass: HomeAssistant) -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_room_off_drops_waiting_restore(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turning the room off while a restore waits for the fade keeps it off."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    await setup_integration()
    await _switch(hass, "turn_on")
    await _step_and_fade(hass, freezer)
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")

    stop = hass.async_create_task(_call_switch(hass, "turn_off"))
    await _settle(hass)
    room_off = hass.async_create_task(_room(hass).async_turn_off())
    await _settle(hass)
    freezer.tick(2)
    await _later(hass, 0)
    await stop
    await room_off

    assert on_calls == []
    assert [call.data for call in off_calls] == [{"entity_id": [lamp]}]


async def test_turned_on_again_before_restore(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Off and on again quickly keeps disco, and what it restores later."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    white = add_light(
        "white", area="Wohnzimmer", state=STATE_ON, attributes=_ct(WHITE_LIGHT, 2700)
    )
    await setup_integration()
    await _switch(hass, "turn_on")
    hass.states.async_set(lamp, STATE_ON, _hs(RAINBOW[0]))
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    await _step_and_fade(hass, freezer)
    calls = async_mock_service(hass, "light", "turn_on")

    stop = hass.async_create_task(_call_switch(hass, "turn_off"))
    await _settle(hass)
    start = hass.async_create_task(_call_switch(hass, "turn_on"))
    await _settle(hass)
    freezer.tick(2)
    await _later(hass, 0)
    await stop
    await start

    assert hass.states.get(DISCO).state == STATE_ON
    # No restore went out, only disco colors.
    assert all("hs_color" in call.data for call in calls)
    assert all(call.data["entity_id"] == [lamp] for call in calls)

    calls.clear()
    # A step, then its fade has ended.
    freezer.tick(10)
    await _later(hass, 0)
    freezer.tick(2)
    await _later(hass, 0)
    await _switch(hass, "turn_off")
    assert sorted((call.data for call in calls), key=_entity)[-2:] == [
        {"entity_id": [lamp], "brightness": 100, "hs_color": (0, 50)},
        {"entity_id": [white], "brightness": 100, "color_temp_kelvin": 2700},
    ]


async def test_late_reports_of_white_lights(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A white light reporting disco's turn off late still comes back."""
    add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    white = add_light(
        "white", area="Wohnzimmer", state=STATE_ON, attributes=_ct(WHITE_LIGHT, 2700)
    )
    await setup_integration()
    await _switch(hass, "turn_on")
    # Reported without our context, e.g. on for a moment, then off.
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    hass.states.async_set(white, STATE_ON, _ct(WHITE_LIGHT, 2700))
    hass.states.async_set(white, STATE_OFF, WHITE_LIGHT)
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "light", "turn_on")

    await _switch(hass, "turn_off")

    assert {"entity_id": [white], "brightness": 100, "color_temp_kelvin": 2700} in [
        call.data for call in calls
    ]


async def _off_then_on(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> tuple[asyncio.Task, asyncio.Task]:
    """Turn disco off during a fade, and on again while the restore waits."""
    await _step_and_fade(hass, freezer)
    stop = hass.async_create_task(_call_switch(hass, "turn_off"))
    await _settle(hass)
    start = hass.async_create_task(_call_switch(hass, "turn_on"))
    await _settle(hass)
    return stop, start


async def test_off_on_off_ends_off(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The last turn off wins, and restores."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    await setup_integration()
    await _switch(hass, "turn_on")
    stop, start = await _off_then_on(hass, freezer)
    calls = async_mock_service(hass, "light", "turn_on")
    last_stop = hass.async_create_task(_call_switch(hass, "turn_off"))
    await _settle(hass)

    freezer.tick(2)
    await _later(hass, 0)
    await asyncio.gather(stop, start, last_stop)
    await _later(hass, 10)

    assert hass.states.get(DISCO).state == STATE_OFF
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "brightness": 100, "hs_color": (0, 50)}
    ]


async def test_off_on_room_off_stays_off(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turning the room off while disco waits to start again keeps it off."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    await setup_integration()
    await _switch(hass, "turn_on")
    stop, start = await _off_then_on(hass, freezer)
    on_calls = async_mock_service(hass, "light", "turn_on")
    off_calls = async_mock_service(hass, "light", "turn_off")
    room_off = hass.async_create_task(_room(hass).async_turn_off())
    await _settle(hass)

    freezer.tick(2)
    await _later(hass, 0)
    await asyncio.gather(stop, start, room_off)
    await _later(hass, 10)

    assert hass.states.get(DISCO).state == STATE_OFF
    assert on_calls == []
    assert [call.data for call in off_calls] == [{"entity_id": [lamp]}]


async def test_unload_while_start_waits(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Unloaded while disco waits to start again, it does not start."""
    add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_hs((0, 50)))
    entry = await setup_integration()
    await _switch(hass, "turn_on")
    stop, start = await _off_then_on(hass, freezer)
    calls = async_mock_service(hass, "light", "turn_on")

    await hass.config_entries.async_unload(entry.entry_id)
    await asyncio.gather(stop, start, return_exceptions=True)
    freezer.tick(2)
    await _later(hass, 0)
    await _later(hass, 20)

    assert calls == []


async def test_lights_in_unknown_state(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Colored lights not reported yet turn off again, white ones are left."""
    add_light("lamp", area="Wohnzimmer", state=STATE_ON)
    unknown = add_light("unknown", area="Wohnzimmer", state=STATE_UNKNOWN)
    add_light(
        "unknown_white", area="Wohnzimmer", state=STATE_UNKNOWN, attributes=WHITE_LIGHT
    )
    await setup_integration()
    await _switch(hass, "turn_on")
    hass.states.async_set(unknown, STATE_ON, _hs(RAINBOW[1]))
    await hass.async_block_till_done()
    off_calls = async_mock_service(hass, "light", "turn_off")

    await _switch(hass, "turn_off")

    assert [call.data for call in off_calls] == [{"entity_id": [unknown]}]
