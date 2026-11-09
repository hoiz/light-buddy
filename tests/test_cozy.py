"""Tests for the cozy mode."""

import datetime as dt
from datetime import timedelta
from http import HTTPStatus

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_buddy.automation import hass_sun
from custom_components.light_buddy.chart import (
    COZY_DASH,
    URL,
    encode_points,
    render_svg,
)
from custom_components.light_buddy.schedule import (
    DEFAULT_SCHEDULE,
    MIRED,
    NOON,
    SUNRISE,
    Sun,
    sun_schedule,
    target_at,
)
from homeassistant.components.light import ColorMode
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT

COZY = "switch.cozy_mode"
TARGET = "sensor.target_color_temperature"
# Reaches every color temperature of the tests.
WIDE_LIGHT = {
    **COLOR_LIGHT,
    "min_color_temp_kelvin": 2000,
    "max_color_temp_kelvin": 6500,
}
COZY_SCHEDULE = sun_schedule(2400, 3500, boost=False)


@pytest.fixture(autouse=True)
def _noon(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-26 12:30:00-07:00")


def _on(kelvin: int) -> dict:
    return {
        **WIDE_LIGHT,
        "color_mode": ColorMode.COLOR_TEMP,
        "color_temp_kelvin": kelvin,
        "brightness": 100,
    }


def _target(hass: HomeAssistant, points) -> int:
    return target_at(points, dt_util.now(), hass_sun(hass)).kelvin


def _sensor(hass: HomeAssistant) -> int:
    return int(hass.states.get(TARGET).state)


async def _wait(hass: HomeAssistant, freezer: FrozenDateTimeFactory, minutes: float):
    """Let time pass, a minute at a time, as the schedule ticks."""
    for _ in range(int(minutes)):
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def _switch(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "switch", service, {"entity_id": COZY}, blocking=True
    )
    await hass.async_block_till_done()


def test_cozy_points() -> None:
    """Cozy mode has no morning boost, is coldest at noon and warmer at sunset."""
    assert [point.sun_event for point in COZY_SCHEDULE[:2]] == [SUNRISE, NOON]
    assert [point.kelvin for point in COZY_SCHEDULE] == [2400, 3500, 2690, 2400]


async def test_switch_blends_to_cozy_and_back(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """The target moves to the cozy curve over ten minutes, and back."""
    lamp = add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(5000))
    entry = await setup_integration(schedule=True, sun=True)
    calls = async_mock_service(hass, "light", "turn_on")
    await _wait(hass, freezer, 1)
    normal = _target(hass, DEFAULT_SCHEDULE)
    assert _sensor(hass) == normal
    assert hass.states.get(COZY).state == STATE_OFF

    await _switch(hass, "turn_on")
    assert hass.states.get(COZY).state == STATE_ON
    assert dt_util.parse_datetime(entry.options["cozy_since"]) == dt_util.now()

    await _wait(hass, freezer, 5)
    halfway = _sensor(hass)
    normal, cozy = _target(hass, DEFAULT_SCHEDULE), _target(hass, COZY_SCHEDULE)
    assert cozy < halfway < normal
    # Even in mired.
    assert MIRED / halfway == pytest.approx(
        (MIRED / normal + MIRED / cozy) / 2, rel=0.01
    )

    await _wait(hass, freezer, 6)
    assert _sensor(hass) == _target(hass, COZY_SCHEDULE)
    assert calls[-1].data["entity_id"] == [lamp]
    assert calls[-1].data["color_temp_kelvin"] == _sensor(hass)
    # Steps, not a jump.
    assert len(calls) >= 5

    await _switch(hass, "turn_off")
    assert hass.states.get(COZY).state == STATE_OFF
    assert "cozy_since" not in entry.options
    await _wait(hass, freezer, 5)
    assert (
        _target(hass, COZY_SCHEDULE) < _sensor(hass) < _target(hass, DEFAULT_SCHEDULE)
    )
    await _wait(hass, freezer, 6)
    assert _sensor(hass) == _target(hass, DEFAULT_SCHEDULE)


async def test_turned_off_midway_turns_back_smoothly(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """Turned off during the blend, it turns back from where it is."""
    add_light("lamp", area="Wohnzimmer")
    await setup_integration(schedule=True, sun=True)
    await _switch(hass, "turn_on")
    await _wait(hass, freezer, 5)
    before = _sensor(hass)

    await _switch(hass, "turn_off")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert abs(_sensor(hass) - before) < 20

    # On its way back, reaching the normal curve after the blend.
    values = [before]
    for _ in range(10):
        await _wait(hass, freezer, 1)
        values.append(_sensor(hass))
    assert values == sorted(values)
    assert values[-1] == _target(hass, DEFAULT_SCHEDULE)


async def test_off_at_night(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """Cozy mode ends by itself at 04:00."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration(schedule=True, sun=True)
    await _switch(hass, "turn_on")

    freezer.move_to("2026-10-27 03:59:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(COZY).state == STATE_ON

    freezer.move_to("2026-10-27 04:00:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(COZY).state == STATE_OFF
    assert "cozy_since" not in entry.options


@pytest.mark.parametrize(
    ("since", "state"),
    [
        ("2026-10-26T09:00:00-07:00", STATE_ON),
        # Turned on after midnight, it lasts the day.
        ("2026-10-26T03:00:00-07:00", STATE_ON),
        ("2026-10-25T20:00:00-07:00", STATE_OFF),
        ("2026-10-25T03:00:00-07:00", STATE_OFF),
        # Unreadable.
        ("2026-10-26T09:00:00", STATE_OFF),
        ("junk", STATE_OFF),
        (12, STATE_OFF),
    ],
)
async def test_kept_across_restarts(
    hass: HomeAssistant, add_light, setup_integration, since, state: str
) -> None:
    """Cozy mode stays on after a restart, unless its 04:00 has passed."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration({"cozy_since": since}, schedule=True, sun=True)

    assert hass.states.get(COZY).state == state
    assert ("cozy_since" in entry.options) is (state == STATE_ON)
    if state == STATE_ON:
        # Back on its curve right away, without a blend.
        assert _sensor(hass) == _target(hass, COZY_SCHEDULE)


async def test_paused_room_is_left_alone(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """A room with a color set by hand keeps it in cozy mode, too."""
    add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(5000))
    entry = await setup_integration(schedule=True, sun=True)
    calls = async_mock_service(hass, "light", "turn_on")
    entry.runtime_data._automations["wohnzimmer"].async_mark_manual()

    await _switch(hass, "turn_on")
    await _wait(hass, freezer, 11)
    assert calls == []


async def test_cozy_settings_apply(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Changed cozy values apply right away."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration({"cozy_since": dt_util.now().isoformat()}, sun=True)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "settings": {"cozy_warmest": 2200, "cozy_coldest": 3000},
        },
    )
    await hass.async_block_till_done()
    assert _sensor(hass) == _target(hass, sun_schedule(2200, 3000, boost=False))


def test_chart_shows_cozy_curve() -> None:
    """The cozy curve is drawn dashed, with a label."""
    now = dt.datetime(2026, 10, 26, 15, 10, tzinfo=dt.UTC)
    svg = render_svg(
        DEFAULT_SCHEDULE, now, "de", Sun(52.52, 13.40), cozy_points=COZY_SCHEDULE
    )
    assert f'stroke-dasharray="{COZY_DASH}"' in svg
    assert "Gemütlicher Modus" in svg
    # The axis reaches the cozy warmest value.
    assert ">2000 K<" in svg
    assert "Gemütlicher Modus" not in render_svg(DEFAULT_SCHEDULE, now, "de")


async def test_chart_view_with_cozy(
    hass: HomeAssistant, hass_client_no_auth, setup_integration
) -> None:
    """The view draws the cozy points it is given, and refuses invalid ones."""
    await setup_integration()
    client = await hass_client_no_auth()
    params = {"points": encode_points(DEFAULT_SCHEDULE), "lang": "en"}

    response = await client.get(
        URL, params={**params, "cozy": encode_points(COZY_SCHEDULE)}
    )
    assert response.status == HTTPStatus.OK
    assert "Cozy mode" in await response.text()

    response = await client.get(URL, params={**params, "cozy": "junk"})
    assert response.status == HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    ("now", "since", "state"),
    [
        # Before 04:00, the previous night's 04:00 counts.
        ("2026-10-27 02:00:00-07:00", "2026-10-26T22:00:00-07:00", STATE_ON),
        ("2026-10-27 02:00:00-07:00", "2026-10-25T22:00:00-07:00", STATE_OFF),
        # Across the end of daylight saving time.
        ("2026-11-01 03:30:00-08:00", "2026-10-31T21:00:00-07:00", STATE_ON),
        ("2026-11-01 04:00:00-08:00", "2026-10-31T21:00:00-07:00", STATE_OFF),
    ],
)
async def test_restart_at_night(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    freezer: FrozenDateTimeFactory,
    now: str,
    since: str,
    state: str,
) -> None:
    """A restart at night keeps cozy mode until its 04:00."""
    freezer.move_to(now)
    add_light("lamp", area="Wohnzimmer")
    await setup_integration({"cozy_since": since}, sun=True)
    assert hass.states.get(COZY).state == state


async def test_turned_on_after_midnight_lasts_the_day(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """Turned on at 01:00, the 04:00 that follows does not end it."""
    freezer.move_to("2026-10-27 01:00:00-07:00")
    add_light("lamp", area="Wohnzimmer")
    await setup_integration(sun=True)
    await _switch(hass, "turn_on")

    freezer.move_to("2026-10-27 04:00:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(COZY).state == STATE_ON

    freezer.move_to("2026-10-28 04:00:00-07:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(COZY).state == STATE_OFF


async def test_toggle_needs_no_reload(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """Storing cozy mode does not reload, nor send anything once settled."""
    add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(5000))
    entry = await setup_integration(schedule=True, sun=True)
    manager = entry.runtime_data
    await _switch(hass, "turn_on")
    await _wait(hass, freezer, 11)
    assert entry.runtime_data is manager

    calls = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.lamp", STATE_ON, _on(_target(hass, COZY_SCHEDULE)))
    await hass.async_block_till_done()
    await _switch(hass, "turn_on")
    assert calls == []


async def test_schedule_off_and_disco_are_left_alone(
    hass: HomeAssistant, add_light, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """Rooms with the schedule off, or during disco, get nothing from cozy mode."""
    add_light("lamp", area="Wohnzimmer", state=STATE_ON, attributes=_on(5000))
    add_light("other", area="Küche", state=STATE_ON, attributes=_on(5000))
    entry = await setup_integration(schedule=True, sun=True)
    automations = entry.runtime_data._automations
    await automations["wohnzimmer"].async_set_enabled(False)
    # As while disco runs, without its color changes.
    automations["kuche"].disco.running = True
    calls = async_mock_service(hass, "light", "turn_on")

    await _switch(hass, "turn_on")
    await _wait(hass, freezer, 11)
    assert calls == []
