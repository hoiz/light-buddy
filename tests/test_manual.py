"""Tests for respecting colors set by hand."""

import asyncio
import dataclasses
import datetime as dt
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_buddy.automation import ScheduleSettings
from custom_components.light_buddy.schedule import SchedulePoint
from homeassistant.components.light import DATA_COMPONENT, ColorMode
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT

ROOM = "light.all_wohnzimmer"
SCHEDULE = "switch.schedule_wohnzimmer"
RESUME = "button.resume_schedule_wohnzimmer"
# Reaches every color temperature of the tests.
WIDE_LIGHT = {**COLOR_LIGHT, "max_color_temp_kelvin": 6500}


@pytest.fixture(autouse=True)
def _noon(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-26 12:30:00-07:00")


def _on(kelvin: int, brightness: int = 100) -> dict:
    return {
        **WIDE_LIGHT,
        "color_mode": ColorMode.COLOR_TEMP,
        "color_temp_kelvin": kelvin,
        "brightness": brightness,
    }


def _curve(kelvin: int) -> tuple[SchedulePoint, ...]:
    """A flat curve."""
    return (SchedulePoint(kelvin, dt.time(0, 0)), SchedulePoint(kelvin, dt.time(12, 0)))


async def _tick(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()


async def _setup_room(hass: HomeAssistant, add_light, setup_integration, *lights):
    """Set up a room with lights that are on, and a flat curve at 4000 K."""
    entity_ids = [
        add_light(name, area="Wohnzimmer", state=STATE_ON, attributes=_on(4000))
        for name in lights
    ]
    entry = await setup_integration(schedule=True)
    automation = entry.runtime_data._automations["wohnzimmer"]
    automation.async_set_points(_curve(4000))
    return automation, entity_ids


def _room(hass: HomeAssistant):
    return hass.data[DATA_COMPONENT].get_entity(ROOM)


def _age_last_sent(hass: HomeAssistant, lamp: str) -> None:
    """Pretend the last command to a light was sent a while ago."""
    sent = _room(hass)._commander.last_sent
    sent[lamp] = dataclasses.replace(sent[lamp], sent_at=sent[lamp].sent_at - 10)


async def _sent_target(hass: HomeAssistant, automation, lamp: str) -> None:
    """Have the schedule send its target to a light, a while ago."""
    hass.states.async_set(lamp, STATE_ON, _on(3000))
    await hass.async_block_till_done()
    await automation.async_resume()
    _age_last_sent(hass, lamp)


@pytest.mark.parametrize(
    "params", [{"color_temp_kelvin": 2700}, {"hs_color": (0, 100)}]
)
async def test_color_through_room_pauses(
    hass: HomeAssistant, add_light, setup_integration, params: dict
) -> None:
    """A color temperature or color set through the room is left alone."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(**params)
    assert automation.paused
    attributes = hass.states.get(SCHEDULE).attributes
    assert attributes["paused"] is True
    assert attributes["paused_since"] is not None

    hass.states.async_set(lamp, STATE_ON, _on(2700), context=calls[0].context)
    automation.async_set_points(_curve(5000))
    await _tick(hass)
    assert len(calls) == 1


async def test_dimming_does_not_pause(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Dimming is no change of color, the schedule goes on."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(brightness=30)
    assert not automation.paused
    hass.states.async_set(lamp, STATE_ON, _on(4000, 30), context=calls[0].context)
    automation.async_set_points(_curve(5000))
    await _tick(hass)
    assert calls[-1].data == {
        "entity_id": [lamp],
        "color_temp_kelvin": 5000,
        "transition": 2,
    }


async def test_bare_turn_on_does_not_pause(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning the room on without values is no change by hand."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on()
    assert not automation.paused


@pytest.mark.parametrize(
    ("service_data", "paused"),
    [
        ({"entity_id": "light.lamp", "color_temp_kelvin": 2700}, True),
        ({"area_id": "wohnzimmer", "color_name": "red"}, True),
        ({"entity_id": "light.lamp", "brightness_pct": 30}, False),
        ({"entity_id": "light.lamp"}, False),
    ],
)
async def test_calls_by_others(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    service_data: dict,
    paused: bool,
) -> None:
    """light.turn_on calls by others with a color pause the schedule."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")

    await hass.services.async_call("light", "turn_on", service_data, blocking=True)
    assert automation.paused is paused


async def test_toggle_counts_for_lights_it_turns_on(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A toggle with a color pauses only when it turns a light on."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "toggle")

    await hass.services.async_call(
        "light", "toggle", {"entity_id": lamp, "color_temp_kelvin": 2700}, blocking=True
    )
    assert not automation.paused

    hass.states.async_set(lamp, STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "light", "toggle", {"entity_id": lamp, "color_temp_kelvin": 2700}, blocking=True
    )
    assert automation.paused


async def test_scene_pauses(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Scenes apply their states through light.turn_on, which is respected."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    assert await async_setup_component(hass, "scene", {})
    await hass.async_block_till_done()

    await hass.services.async_call(
        "scene",
        "apply",
        {"entities": {lamp: {"state": "on", "color_temp_kelvin": 2700}}},
        blocking=True,
    )
    assert automation.paused


async def test_changes_outside_home_assistant(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A remote changing the color counts; transitions, small drifts and dimming not."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(lamp, STATE_ON, _on(3000))
    await hass.async_block_till_done()
    await automation.async_resume()

    # Still within the transition: values from mid transition.
    hass.states.async_set(lamp, STATE_ON, _on(3000), context=Context())
    await hass.async_block_till_done()
    assert not automation.paused

    _age_last_sent(hass, lamp)
    for attributes in (_on(3950), _on(4000, 20)):
        hass.states.async_set(lamp, STATE_ON, attributes, context=Context())
        await hass.async_block_till_done()
        assert not automation.paused

    hass.states.async_set(lamp, STATE_ON, _on(3000), context=Context())
    await hass.async_block_till_done()
    assert automation.paused


async def test_own_reports_are_not_manual(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """State reports carrying our own context are no changes by hand."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    calls = async_mock_service(hass, "light", "turn_on")
    await _sent_target(hass, automation, lamp)

    hass.states.async_set(lamp, STATE_ON, _on(2700), context=calls[0].context)
    await hass.async_block_till_done()
    assert not automation.paused


async def test_lights_without_color_temp(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights that cannot show a color temperature never pause it."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    dimmer = add_light(
        "dimmer",
        area="Wohnzimmer",
        state=STATE_ON,
        attributes={
            "supported_color_modes": [ColorMode.BRIGHTNESS],
            "color_mode": ColorMode.BRIGHTNESS,
            "brightness": 100,
        },
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    async_mock_service(hass, "light", "turn_on")
    # A color temperature for the whole room reaches the dimmer, too.
    await _room(hass)._commander.async_send_now({dimmer: {"color_temp_kelvin": 3000}})
    _age_last_sent(hass, dimmer)

    hass.states.async_set(
        dimmer,
        STATE_ON,
        {
            "supported_color_modes": [ColorMode.BRIGHTNESS],
            "color_mode": ColorMode.BRIGHTNESS,
            "brightness": 101,
        },
        context=Context(),
    )
    await hass.async_block_till_done()
    assert not automation.paused


async def test_pause_ends_when_room_is_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning all lights off ends the pause, the next turn on starts fresh."""
    automation, lamps = await _setup_room(hass, add_light, setup_integration, "a", "b")
    calls = async_mock_service(hass, "light", "turn_on")
    automation.async_mark_manual()

    # One light off, the other on: the room is still on, and paused.
    hass.states.async_set(lamps[0], STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    await _room(hass).async_turn_on()
    assert calls[-1].data == {"entity_id": [lamps[0]]}

    for lamp in lamps:
        hass.states.async_set(lamp, STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    assert not automation.paused
    assert hass.states.get(SCHEDULE).attributes["paused"] is False

    await _room(hass).async_turn_on()
    # Sent once the room's command interval has passed.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": lamps, "color_temp_kelvin": 4000}


async def test_quick_off_and_on_keeps_pause(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A color set right after turning off stays paused."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    room = _room(hass)

    await room.async_turn_off()
    # Within the command interval, so the turn on waits.
    await room.async_turn_on(color_temp_kelvin=2700)
    hass.states.async_set(lamp, STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    assert automation.paused


async def test_pause_ends_on_tick_if_room_stayed_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A pause left from a dropped turn on ends at a later tick."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    hass.states.async_set(lamp, STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    automation.async_mark_manual()

    # A fresh pause may belong to a light about to report on.
    await _tick(hass)
    assert automation.paused

    automation._paused_at -= 60
    await _tick(hass)
    assert not automation.paused


async def test_unavailable_room_keeps_pause(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights that are unavailable, e.g. during a restart, keep the pause."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    automation.async_mark_manual()
    automation._paused_at -= 60
    hass.states.async_set(lamp, "unavailable", WIDE_LIGHT)
    await hass.async_block_till_done()

    await _tick(hass)
    assert automation.paused


async def test_resume_button(hass: HomeAssistant, add_light, setup_integration) -> None:
    """The resume button ends the pause and applies the target at once."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    hass.states.async_set(lamp, STATE_ON, _on(2700))
    await hass.async_block_till_done()
    automation.async_mark_manual()
    calls = async_mock_service(hass, "light", "turn_on")

    await hass.services.async_call(
        "button", "press", {"entity_id": RESUME}, blocking=True
    )
    assert not automation.paused
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "color_temp_kelvin": 4000, "transition": 2}
    ]
    assert hass.states.get(RESUME).name == "Resume schedule Wohnzimmer"


async def test_resume_does_not_pause_again(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After resuming, a light left in a color does not pause the room again."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    await _sent_target(hass, automation, lamp)
    red = {**WIDE_LIGHT, "color_mode": ColorMode.XY, "brightness": 128}
    hass.states.async_set(lamp, STATE_ON, red, context=Context())
    await hass.async_block_till_done()
    assert automation.paused

    await automation.async_resume()
    hass.states.async_set(lamp, STATE_ON, {**red, "brightness": 129}, context=Context())
    await hass.async_block_till_done()
    assert not automation.paused


async def test_after_resume_remote_changes_count(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After resuming, lights already at the target still notice remotes."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    automation.async_mark_manual()

    # Nothing to send, the light shows the target already.
    await automation.async_resume()
    _age_last_sent(hass, lamp)
    hass.states.async_set(lamp, STATE_ON, _on(2700), context=Context())
    await hass.async_block_till_done()
    assert automation.paused


async def test_resume_keeps_running_fades(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Resuming during a fade keeps comparing with where it is heading."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    commander = _room(hass)._commander
    await commander.async_send_now(
        {lamp: {"color_temp_kelvin": 3000, "transition": 10}}
    )
    automation.async_mark_manual()

    automation._async_clear_pause()
    assert commander.last_sent[lamp].value == 3000
    assert commander.last_sent[lamp].transition == 10


async def test_light_off_forgets_what_was_sent(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After a light was off, its old values are not compared any more."""
    automation, lamps = await _setup_room(hass, add_light, setup_integration, "a", "b")
    async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(lamps[0], STATE_ON, _on(3000))
    await hass.async_block_till_done()
    await automation.async_resume()
    assert lamps[0] in _room(hass)._commander.last_sent

    hass.states.async_set(lamps[0], STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    assert lamps[0] not in _room(hass)._commander.last_sent


async def test_older_transition_still_counts(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A long color temperature fade is not cut short by a later brightness."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    commander = _room(hass)._commander
    await commander.async_send_now(
        {lamp: {"color_temp_kelvin": 2700, "transition": 60}}
    )
    await commander.async_send_now({lamp: {"brightness": 100}})

    # Halfway through the 60 s fade.
    hass.states.async_set(lamp, STATE_ON, _on(3400), context=Context())
    await hass.async_block_till_done()
    assert not automation.paused


async def test_white_replaces_color_temp(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After sending white, there is no color temperature to compare."""
    _, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    commander = _room(hass)._commander
    await commander.async_send_now({lamp: {"color_temp_kelvin": 4000}})
    await commander.async_send_now({lamp: {"white": 200}})
    assert lamp not in commander.last_sent


async def test_auto_resume(hass: HomeAssistant, add_light, setup_integration) -> None:
    """With a timeout, the pause ends by itself and the target is applied."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    automation.async_set_settings(ScheduleSettings(auto_resume_minutes=10))
    hass.states.async_set(lamp, STATE_ON, _on(2700))
    await hass.async_block_till_done()
    automation.async_mark_manual()
    calls = async_mock_service(hass, "light", "turn_on")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await hass.async_block_till_done()
    assert not automation.paused
    assert calls[-1].data["color_temp_kelvin"] == 4000


async def test_repeated_reports_do_not_delay_resume(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light that keeps reporting the same color does not delay resuming."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    await _sent_target(hass, automation, lamp)
    hass.states.async_set(lamp, STATE_ON, _on(2700), context=Context())
    await hass.async_block_till_done()
    assert automation.paused
    automation._paused_at -= 100
    paused_at = automation._paused_at

    # The same color again, with another brightness: no new change.
    hass.states.async_set(lamp, STATE_ON, _on(2700, 50), context=Context())
    await hass.async_block_till_done()
    assert automation._paused_at == paused_at

    # Turning the color further step by step adds up to a new change.
    for kelvin in (2650, 2600, 2550):
        hass.states.async_set(lamp, STATE_ON, _on(kelvin, 50), context=Context())
        await hass.async_block_till_done()
    assert automation._paused_at > paused_at


async def test_lights_not_answering_do_not_block(
    hass: HomeAssistant, add_light, setup_integration, monkeypatch
) -> None:
    """A light that never answers is given up on, the others get their command."""
    automation, lamps = await _setup_room(
        hass, add_light, setup_integration, "a_hangs", "b_answers"
    )
    monkeypatch.setattr("custom_components.light_buddy.room.CALL_TIMEOUT_SECONDS", 0.05)
    hang = hass.loop.create_future()
    answered = []

    async def turn_on(call) -> None:
        if lamps[0] in call.data["entity_id"]:
            await hang
        answered.append(call.data["entity_id"])

    hass.services.async_register("light", "turn_on", turn_on)
    # Without transitions on the second, the lights get separate calls.
    hass.states.async_set(lamps[0], STATE_ON, _on(3000))
    hass.states.async_set(lamps[1], STATE_ON, {**_on(3000), "supported_features": 0})
    await hass.async_block_till_done()

    # Time is frozen in these tests, so the timeout is moved on by hand.
    resume = hass.async_create_task(automation.async_resume())
    await asyncio.sleep(0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await asyncio.wait_for(resume, 5)
    assert answered == [[lamps[1]]]
    assert hang.cancelled()


async def test_slow_tick_skips_next(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A tick still running makes the next one wait for the one after."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    running = hass.loop.create_future()

    async def slow_tick() -> None:
        await running

    automation._tick = hass.async_create_task(slow_tick())
    tasks = set(automation._tasks)

    automation._async_tick_later(dt_util.utcnow())
    assert automation._tasks == tasks
    running.set_result(None)
    await hass.async_block_till_done()


async def test_room_fading_out_is_not_manual(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Reports while the room fades out do not pause it."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    await _sent_target(hass, automation, lamp)

    await _room(hass).async_turn_off(transition=10)
    hass.states.async_set(lamp, STATE_ON, _on(3000, 20), context=Context())
    await hass.async_block_till_done()
    assert not automation.paused


async def test_resume_forgets_lights_without_color_temp_shown(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light showing no color temperature yet has nothing to compare."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    await _sent_target(hass, automation, lamp)
    hass.states.async_set(
        lamp, STATE_ON, {**_on(4000), "color_temp_kelvin": None}, context=Context()
    )
    await hass.async_block_till_done()

    automation._async_clear_pause()
    assert lamp not in _room(hass)._commander.last_sent


async def test_turning_on_ends_fade_out_window(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After turning the room back on, colors set by hand count again."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    await _sent_target(hass, automation, lamp)
    room = _room(hass)

    await room.async_turn_off(transition=30)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    await room.async_turn_on()
    hass.states.async_set(lamp, STATE_ON, _on(2700), context=Context())
    await hass.async_block_till_done()
    assert automation.paused
