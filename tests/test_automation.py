"""Tests for following the schedule."""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components.light import DATA_COMPONENT, ColorMode
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT, WHITE_LIGHT

ROOM = "light.all_wohnzimmer"
SCHEDULE = "switch.schedule_wohnzimmer"
TARGET = "sensor.target_color_temperature_wohnzimmer"
# Coldest point of the default schedule, 12:30 in the test time zone.
NOON = "2026-10-26 12:30:00-07:00"
ON_CT = {"color_mode": ColorMode.COLOR_TEMP, "brightness": 100}


@pytest.fixture(autouse=True)
def _noon(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOON)


async def _tick(hass: HomeAssistant) -> None:
    """Let every room start and run its schedule once."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()


def _on(attributes: dict, kelvin: int) -> dict:
    return {**attributes, **ON_CT, "color_temp_kelvin": kelvin}


async def test_entities(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Each room gets a schedule switch and a target sensor."""
    add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True)

    switch = hass.states.get(SCHEDULE)
    assert switch.state == STATE_ON
    assert switch.name == "Schedule Wohnzimmer"
    sensor = hass.states.get(TARGET)
    assert sensor.state == "5000"
    assert sensor.attributes["unit_of_measurement"] == "K"


async def test_tick_updates_lights_that_are_on(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """The tick brings lights showing a color temperature to the target."""
    # Already on, so they are not seen as turned on elsewhere.
    plain = add_light(
        "plain", area="Wohnzimmer", state=STATE_ON, attributes=_on(WHITE_LIGHT, 3000)
    )
    cold = add_light(
        "cold", area="Wohnzimmer", state=STATE_ON, attributes=_on(WHITE_LIGHT, 2700)
    )
    # Within the threshold of 50 K.
    add_light(
        "near", area="Wohnzimmer", state=STATE_ON, attributes=_on(WHITE_LIGHT, 4980)
    )
    # Someone chose a color, which the schedule leaves alone.
    add_light(
        "color",
        area="Wohnzimmer",
        state=STATE_ON,
        attributes={**COLOR_LIGHT, "color_mode": ColorMode.XY, "brightness": 100},
    )
    # Can only reach 4000 K, which it already shows.
    add_light(
        "limited",
        area="Wohnzimmer",
        state=STATE_ON,
        attributes=_on(COLOR_LIGHT, 4000),
    )
    add_light("off", area="Wohnzimmer")
    registry = er.async_get(hass)
    await setup_integration(
        {
            "offsets": {
                registry.async_get(cold).id: {"value": -300, "unit": "K"},
            }
        },
        schedule=True,
    )
    calls = async_mock_service(hass, "light", "turn_on")

    await _tick(hass)

    assert sorted((call.data for call in calls), key=str) == [
        {"entity_id": [cold], "color_temp_kelvin": 4700, "transition": 2},
        {"entity_id": [plain], "color_temp_kelvin": 5000, "transition": 2},
    ]


async def test_turn_on_through_room_uses_target(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights that are off turn on with the target in the same call."""
    off_a = add_light("off_a", area="Wohnzimmer")
    off_b = add_light("off_b", area="Wohnzimmer")
    on = add_light("on", area="Wohnzimmer", state=STATE_ON)
    await setup_integration(schedule=True)
    calls = async_mock_service(hass, "light", "turn_on")
    room = hass.data[DATA_COMPONENT].get_entity(ROOM)

    await room.async_turn_on()
    assert [call.data for call in calls] == [
        {"entity_id": [off_a, off_b], "color_temp_kelvin": 5000}
    ]

    await room.async_turn_on(brightness=100)
    # Sent once the room's command interval has passed.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert [call.data for call in calls][1:] == [
        {"entity_id": [off_a, off_b], "brightness": 100, "color_temp_kelvin": 5000},
        {"entity_id": [on], "brightness": 100},
    ]


async def test_turn_on_with_color_is_left_alone(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A turn on with a color, e.g. from a scene, gets no target added."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True)
    calls = async_mock_service(hass, "light", "turn_on")

    await hass.data[DATA_COMPONENT].get_entity(ROOM).async_turn_on(hs_color=(0, 100))
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "hs_color": (0, 100)}
    ]


@pytest.mark.parametrize("old_state", [STATE_OFF, STATE_UNAVAILABLE])
async def test_light_turned_on_elsewhere(
    hass: HomeAssistant, add_light, setup_integration, old_state: str
) -> None:
    """A light turned on directly or by a wall switch gets the target."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True)
    hass.states.async_set(lamp, old_state, COLOR_LIGHT)
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "light", "turn_on")

    hass.states.async_set(lamp, STATE_ON, _on(COLOR_LIGHT, 2700))
    # Lights turning on together are collected for a moment.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "color_temp_kelvin": 5000, "transition": 1}
    ]


async def test_own_turn_on_is_not_corrected(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights turned on by the room light are not sent the target again."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True)
    calls = async_mock_service(hass, "light", "turn_on")
    context = Context()
    room = hass.data[DATA_COMPONENT].get_entity(ROOM)
    room.async_set_context(context)

    await room.async_turn_on()
    hass.states.async_set(lamp, STATE_ON, _on(COLOR_LIGHT, 5000), context=context)
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_schedule_switch(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Without the schedule, nothing is sent; turning it on applies it."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SCHEDULE}, blocking=True
    )
    hass.states.async_set(lamp, STATE_ON, _on(COLOR_LIGHT, 3000))
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "light", "turn_on")

    await _tick(hass)
    assert calls == []

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SCHEDULE}, blocking=True
    )
    assert [call.data["color_temp_kelvin"] for call in calls] == [5000]


async def test_schedule_switch_is_stored(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Whether the schedule is on is kept in the config entry.

    So it holds across restarts, also while the switch entity is disabled.
    """
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration({"schedules_off": ["wohnzimmer"]}, schedule=True)
    assert hass.states.get(SCHEDULE).state == STATE_OFF

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": SCHEDULE}, blocking=True
    )
    assert entry.options["schedules_off"] == []
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SCHEDULE}, blocking=True
    )
    assert entry.options["schedules_off"] == ["wohnzimmer"]


async def test_target_follows_time(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The sensor shows the interpolated target as time passes."""
    add_light("lamp", area="Wohnzimmer")
    freezer.move_to("2026-10-26 07:15:00-07:00")
    await setup_integration(schedule=True)
    assert hass.states.get(TARGET).state == "3429"

    freezer.move_to("2026-10-26 23:00:00-07:00")
    await _tick(hass)
    assert hass.states.get(TARGET).state == "2200"


async def test_lights_turned_on_together_get_one_command(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights turned on by a wall switch together get a single command."""
    lamps = [add_light(name, area="Wohnzimmer") for name in ("a", "b", "c")]
    await setup_integration(schedule=True)
    calls = async_mock_service(hass, "light", "turn_on")

    for lamp in lamps:
        hass.states.async_set(lamp, STATE_ON, _on(COLOR_LIGHT, 2700))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert [sorted(call.data["entity_id"]) for call in calls] == [sorted(lamps)]


async def test_light_with_color_turned_on_elsewhere(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light turned on with a color, e.g. by a scene, keeps it."""
    lamp = add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True)
    calls = async_mock_service(hass, "light", "turn_on")

    hass.states.async_set(
        lamp, STATE_ON, {**COLOR_LIGHT, "color_mode": ColorMode.XY, "brightness": 100}
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls == []


async def test_no_target_while_turning_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights reporting on right after turning off are not turned on again."""
    lamp = add_light(
        "lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(COLOR_LIGHT, 3000)
    )
    await setup_integration(schedule=True)
    on_calls = async_mock_service(hass, "light", "turn_on")
    async_mock_service(hass, "light", "turn_off")

    # Turned off without the room light, then a short report of on.
    hass.states.async_set(lamp, STATE_OFF, COLOR_LIGHT)
    hass.states.async_set(lamp, STATE_ON, _on(COLOR_LIGHT, 3000))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert on_calls == []

    # The room light turns off with a transition, and the tick runs meanwhile.
    await hass.data[DATA_COMPONENT].get_entity(ROOM).async_turn_off(transition=5)
    await _tick(hass)
    assert on_calls == []


async def test_schedule_changes_apply_without_reload(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A new curve applies right away, and pauses go on."""
    lamp = add_light(
        "lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(WHITE_LIGHT, 5000)
    )
    entry = await setup_integration(schedule=True)
    automation = entry.runtime_data._automations["wohnzimmer"]
    automation.async_mark_manual()
    room = hass.data[DATA_COMPONENT].get_entity(ROOM)

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "schedule": [{"time": "00:00", "kelvin": 3000, "weekdays": []}],
        },
    )
    await hass.async_block_till_done()
    assert hass.states.get(TARGET).state == "3000"
    assert hass.data[DATA_COMPONENT].get_entity(ROOM) is room
    assert automation.paused
    assert hass.states.get(lamp).state == STATE_ON


async def test_settings_apply_right_away(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Threshold and transition settings apply as soon as they are saved."""
    add_light(
        "lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(WHITE_LIGHT, 4920)
    )
    entry = await setup_integration(schedule=True)
    calls = async_mock_service(hass, "light", "turn_on")

    # 80 K off: within a threshold of 100 K.
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "settings": {"threshold": 100, "transition": 4}},
    )
    await hass.async_block_till_done()
    assert calls == []

    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "settings": {"threshold": 50, "transition": 4}},
    )
    await hass.async_block_till_done()
    assert [call.data["transition"] for call in calls] == [4]

    # Options the entities store themselves do not apply the schedule again.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "schedules_off": []}
    )
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_auto_resume_counts_from_last_change(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A shorter automatic resume ends a long running pause right away."""
    from custom_components.light_buddy.automation import ScheduleSettings

    add_light(
        "lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(WHITE_LIGHT, 3000)
    )
    entry = await setup_integration(schedule=True)
    automation = entry.runtime_data._automations["wohnzimmer"]
    automation.async_mark_manual()
    automation._paused_at -= 50 * 60

    automation.async_set_settings(ScheduleSettings(auto_resume_minutes=30))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert not automation.paused
