"""Tests for respecting manual changes."""

import datetime as dt
from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

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
ON_CT = {"color_mode": ColorMode.COLOR_TEMP, "brightness": 100}


@pytest.fixture(autouse=True)
def _noon(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-26 12:30:00-07:00")


# Reaches every color temperature of the tests.
WIDE_LIGHT = {**COLOR_LIGHT, "max_color_temp_kelvin": 6500}


def _on(kelvin: int, brightness: int = 100) -> dict:
    return {
        **WIDE_LIGHT,
        **ON_CT,
        "color_temp_kelvin": kelvin,
        "brightness": brightness,
    }


async def _tick(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()


async def _setup_room(hass: HomeAssistant, add_light, setup_integration, *lights):
    """Set up a room with lights that are on, and a curve with brightness."""
    entity_ids = [
        add_light(name, area="Wohnzimmer", state=STATE_ON, attributes=_on(4000))
        for name in lights
    ]
    entry = await setup_integration(schedule=True)
    automation = entry.runtime_data._automations["wohnzimmer"]
    automation.async_set_points(_curve(4000))
    return automation, entity_ids


def _curve(kelvin: int) -> tuple[SchedulePoint, ...]:
    """A flat curve with brightness, at 50 %."""
    return (
        SchedulePoint(kelvin, dt.time(0, 0), brightness=50),
        SchedulePoint(kelvin, dt.time(12, 0), brightness=50),
    )


def _room(hass: HomeAssistant):
    return hass.data[DATA_COMPONENT].get_entity(ROOM)


async def test_dimming_pauses_brightness_only(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Dimming the room keeps the color temperature on the schedule."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    calls = async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(brightness=200)
    assert automation.paused == {"brightness"}
    attributes = hass.states.get(SCHEDULE).attributes
    assert attributes["paused"] == ["brightness"]
    assert attributes["paused_since"] is not None

    # The light reports the dimmed brightness, the curve moved on to 5000 K.
    hass.states.async_set(lamp, STATE_ON, _on(4000, 200), context=calls[0].context)
    automation.async_set_points(_curve(5000))
    await _tick(hass)
    assert calls[-1].data == {
        "entity_id": [lamp],
        "color_temp_kelvin": 5000,
        "transition": 2,
    }


@pytest.mark.parametrize(
    "params", [{"color_temp_kelvin": 2700}, {"hs_color": (0, 100)}]
)
async def test_color_through_room_pauses_color_temp(
    hass: HomeAssistant, add_light, setup_integration, params: dict
) -> None:
    """A color temperature or color set through the room is left alone."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on(**params)
    assert automation.paused == {"color_temp"}


async def test_bare_turn_on_pauses_nothing(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning the room on without values is not a manual change."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")

    await _room(hass).async_turn_on()
    assert automation.paused == set()


@pytest.mark.parametrize(
    ("target", "service_data", "paused"),
    [
        ({"entity_id": "light.lamp"}, {"color_temp_kelvin": 2700}, {"color_temp"}),
        ({"area_id": "wohnzimmer"}, {"brightness": 30}, {"brightness"}),
        ({"entity_id": "light.lamp"}, {}, set()),
    ],
)
async def test_calls_by_others(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    target: dict,
    service_data: dict,
    paused: set,
) -> None:
    """light.turn_on calls by others to members pause what they set."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")

    await hass.services.async_call(
        "light", "turn_on", {**target, **service_data}, blocking=True
    )
    assert automation.paused == paused


async def test_scene_pauses_color_temp(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
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
    assert automation.paused == {"color_temp"}


async def test_changes_outside_home_assistant(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A remote changing a light counts, our own reports and small drifts not."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    commander = _room(hass)._commander
    hass.states.async_set(lamp, STATE_ON, _on(3000, 20))
    await hass.async_block_till_done()
    # Sends the target, 4000 K at 50 %.
    await automation.async_resume()

    # Still within the transition: values from mid transition.
    hass.states.async_set(lamp, STATE_ON, _on(3000), context=Context())
    await hass.async_block_till_done()
    assert automation.paused == set()

    sent_at, params = commander.last_sent[lamp]
    commander.last_sent[lamp] = (sent_at - 10, params)
    # Close to the 4000 K at 128 that were sent.
    hass.states.async_set(lamp, STATE_ON, _on(3950, 125), context=Context())
    await hass.async_block_till_done()
    assert automation.paused == set()

    hass.states.async_set(lamp, STATE_ON, _on(3000, 20), context=Context())
    await hass.async_block_till_done()
    assert automation.paused == {"color_temp", "brightness"}


async def test_own_reports_are_not_manual(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """State reports carrying our own context are not manual changes."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    calls = async_mock_service(hass, "light", "turn_on")
    commander = _room(hass)._commander
    hass.states.async_set(lamp, STATE_ON, _on(3000, 20))
    await hass.async_block_till_done()
    # Sends the target, 4000 K at 50 %.
    await automation.async_resume()
    sent_at, params = commander.last_sent[lamp]
    commander.last_sent[lamp] = (sent_at - 10, params)

    hass.states.async_set(lamp, STATE_ON, _on(2700), context=calls[0].context)
    await hass.async_block_till_done()
    assert automation.paused == set()


async def test_pause_ends_when_room_is_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning all lights off ends the pause, the next turn on starts fresh."""
    automation, lamps = await _setup_room(hass, add_light, setup_integration, "a", "b")
    calls = async_mock_service(hass, "light", "turn_on")
    automation.async_mark_manual({"color_temp"})

    # One light off, the other on: the room is still on, and paused.
    hass.states.async_set(lamps[0], STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    # Only the color temperature is paused, the brightness still follows.
    await _room(hass).async_turn_on()
    assert calls[-1].data == {"entity_id": [lamps[0]], "brightness": 128}

    for lamp in lamps:
        hass.states.async_set(lamp, STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    assert automation.paused == set()
    assert hass.states.get(SCHEDULE).attributes["paused"] == []

    await _room(hass).async_turn_on()
    # Sent once the room's command interval has passed.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls[-1].data == {
        "entity_id": lamps,
        "color_temp_kelvin": 4000,
        "brightness": 128,
    }


async def test_resume_button(hass: HomeAssistant, add_light, setup_integration) -> None:
    """The resume button ends the pause and applies the target at once."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    hass.states.async_set(lamp, STATE_ON, _on(2700, 30))
    await hass.async_block_till_done()
    automation.async_mark_manual({"color_temp", "brightness"})
    calls = async_mock_service(hass, "light", "turn_on")

    await hass.services.async_call(
        "button", "press", {"entity_id": RESUME}, blocking=True
    )
    assert automation.paused == set()
    assert [call.data for call in calls] == [
        {
            "entity_id": [lamp],
            "color_temp_kelvin": 4000,
            "brightness": 128,
            "transition": 2,
        }
    ]
    assert hass.states.get(RESUME).name == "Resume schedule Wohnzimmer"


def _age_last_sent(hass: HomeAssistant, lamp: str) -> None:
    """Pretend the last command to a light was sent a while ago."""
    commander = _room(hass)._commander
    sent_at, params = commander.last_sent[lamp]
    commander.last_sent[lamp] = (sent_at - 10, params)


async def test_brightness_pct_by_others(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Calls with brightness_pct, as automations use it, are respected."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")

    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.lamp", "brightness_pct": 30},
        blocking=True,
    )
    assert automation.paused == {"brightness"}


async def test_toggle_turning_off_is_not_manual(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A toggle with values that turns a light off pauses nothing."""
    automation, _ = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "toggle")

    await hass.services.async_call(
        "light", "toggle", {"entity_id": "light.lamp", "brightness": 30}, blocking=True
    )
    assert automation.paused == set()


async def test_values_sent_earlier_are_kept(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A command with brightness only keeps the color temperature sent before."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(lamp, STATE_ON, _on(3000, 20))
    await hass.async_block_till_done()
    await automation.async_resume()
    # The light shows the target, then only the brightness of the curve moves.
    hass.states.async_set(lamp, STATE_ON, _on(4000, 128), context=Context())
    automation.async_set_points(
        (
            SchedulePoint(4000, dt.time(0, 0), brightness=80),
            SchedulePoint(4000, dt.time(12, 0), brightness=80),
        )
    )
    await _room(hass).async_apply_target(automation.target, 2, threshold=50)
    assert _room(hass)._commander.last_sent[lamp][1]["color_temp_kelvin"] == 4000

    _age_last_sent(hass, lamp)
    hass.states.async_set(lamp, STATE_ON, _on(2700, 204), context=Context())
    await hass.async_block_till_done()
    assert automation.paused == {"color_temp"}


async def test_resume_does_not_pause_again(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After resuming, a light left in a color does not pause the room again."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    hass.states.async_set(lamp, STATE_ON, _on(3000, 20))
    await hass.async_block_till_done()
    await automation.async_resume()
    _age_last_sent(hass, lamp)
    red = {**WIDE_LIGHT, "color_mode": ColorMode.XY, "brightness": 128}
    hass.states.async_set(lamp, STATE_ON, red, context=Context())
    await hass.async_block_till_done()
    assert automation.paused == {"color_temp"}

    await automation.async_resume()
    hass.states.async_set(lamp, STATE_ON, {**red, "brightness": 129}, context=Context())
    await hass.async_block_till_done()
    assert automation.paused == set()


async def test_light_off_forgets_what_was_sent(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """After a light was off, its old values are not compared any more."""
    automation, lamps = await _setup_room(hass, add_light, setup_integration, "a", "b")
    async_mock_service(hass, "light", "turn_on")
    await automation.async_resume()

    hass.states.async_set(lamps[0], STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    assert lamps[0] not in _room(hass)._commander.last_sent


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
    await _room(hass)._commander.async_send_now(
        {dimmer: {"color_temp_kelvin": 3000, "brightness": 100}}
    )
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
    assert automation.paused == set()


async def test_fading_out_is_not_manual(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Brightness reported while a light fades out is not dimming by hand."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    await automation.async_resume()
    _age_last_sent(hass, lamp)

    await hass.services.async_call(
        "light", "turn_off", {"entity_id": lamp, "transition": 10}, blocking=True
    )
    hass.states.async_set(lamp, STATE_ON, _on(4000, 30), context=Context())
    await hass.async_block_till_done()
    assert automation.paused == set()


async def test_quick_off_and_on_keeps_pause(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A dimmed turn on right after turning off stays paused."""
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")
    room = _room(hass)

    await room.async_turn_off()
    # Within the command interval, so the turn on waits.
    await room.async_turn_on(brightness=50)
    hass.states.async_set(lamp, STATE_OFF, WIDE_LIGHT)
    await hass.async_block_till_done()
    assert automation.paused == {"brightness"}


async def test_auto_resume(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a timeout, the pause ends by itself and the target is applied."""
    monkeypatch.setattr("custom_components.light_buddy.automation.AUTO_RESUME", 600)
    automation, (lamp,) = await _setup_room(hass, add_light, setup_integration, "lamp")
    hass.states.async_set(lamp, STATE_ON, _on(2700, 30))
    await hass.async_block_till_done()
    automation.async_mark_manual({"color_temp"})
    calls = async_mock_service(hass, "light", "turn_on")

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await hass.async_block_till_done()
    assert automation.paused == set()
    assert calls[-1].data["color_temp_kelvin"] == 4000
