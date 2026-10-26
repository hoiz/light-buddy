"""Tests for the schedule math."""

import datetime as dt
import itertools
from zoneinfo import ZoneInfo

import pytest

from custom_components.light_buddy.schedule import (
    DASH,
    DEFAULT_SCHEDULE,
    NOON,
    SUNRISE,
    SUNSET,
    SchedulePoint,
    Sun,
    Target,
    day_times,
    format_when,
    point_time,
    points_from_options,
    points_to_options,
    target_at,
)

TZ = ZoneInfo("Europe/Berlin")
BERLIN = Sun(52.52, 13.40)
SUMMER = dt.date(2026, 6, 22)
# A Monday.
MONDAY = dt.date(2026, 10, 26)


def _at(hour: int, minute: int = 0, day: dt.date = MONDAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=TZ)


def test_interpolates_in_mired() -> None:
    """Halfway in time is halfway in mired, not in Kelvin."""
    points = [SchedulePoint(3000, dt.time(6, 30)), SchedulePoint(4000, dt.time(8, 0))]
    # 333.3 and 250 mired, halfway is 291.7 mired.
    assert target_at(points, _at(7, 15)) == Target(3429)
    assert target_at(points, _at(6, 30)) == Target(3000)


def test_default_schedule() -> None:
    """The default follows the sun, and stays at 2700 K at night."""
    noon = point_time(SchedulePoint(0, sun_event=NOON), MONDAY, TZ, BERLIN)
    assert noon.strftime("%H:%M") == "11:50"
    assert target_at(DEFAULT_SCHEDULE, noon, BERLIN) == Target(5000)
    # Sunset is at 16:48: warm soon after.
    assert target_at(DEFAULT_SCHEDULE, _at(18, 0), BERLIN).kelvin < 2850
    assert target_at(DEFAULT_SCHEDULE, _at(23, 0), BERLIN) == Target(2700)
    assert target_at(DEFAULT_SCHEDULE, _at(3, 0), BERLIN) == Target(2700)


def test_across_midnight() -> None:
    """Interpolation continues from the last point of a day to the next day."""
    points = [SchedulePoint(2000, dt.time(22, 0)), SchedulePoint(3000, dt.time(2, 0))]
    # 500 and 333.3 mired, halfway at midnight is 416.7 mired.
    assert target_at(points, _at(0, 0)) == Target(2400)


def test_weekdays() -> None:
    """Points limited to weekdays only apply on those days."""
    points = [
        SchedulePoint(5000, dt.time(7, 0), weekdays=frozenset(range(5))),
        SchedulePoint(2700, dt.time(9, 0), weekdays=frozenset({5, 6})),
        SchedulePoint(2200, dt.time(22, 0)),
    ]
    assert target_at(points, _at(7, 0)) == Target(5000)
    saturday = MONDAY + dt.timedelta(days=5)
    # Between Friday 22:00 and Saturday 09:00, on the way to 2700 K.
    assert 2200 < target_at(points, _at(7, 0, saturday)).kelvin < 2700


def test_sun_point() -> None:
    """Sun points use the day's sun time plus their offset."""
    point = SchedulePoint(2700, sun_event=SUNSET, offset_minutes=-30)
    # Sunset in Berlin on 26 October is at 16:48.
    assert point_time(point, MONDAY, TZ, BERLIN).strftime("%H:%M") == "16:18"
    points = [SchedulePoint(4000, dt.time(12, 0)), point]
    assert target_at(points, _at(16, 20), BERLIN) == Target(2700)
    # Without the sun, only the clock point counts.
    assert target_at(points, _at(16, 20)) == Target(4000)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # Sunrise at 06:51 is inside the window.
        (MONDAY, "06:51"),
        # Sunrise at 04:44 is before the window.
        (SUMMER, "06:00"),
        # Sunrise at 08:15 is after it.
        (dt.date(2026, 12, 21), "07:30"),
    ],
)
def test_window(day: dt.date, expected: str) -> None:
    """Sun points are kept within their window."""
    point = SchedulePoint(
        2700, sun_event=SUNRISE, earliest=dt.time(6, 0), latest=dt.time(7, 30)
    )
    assert point_time(point, day, TZ, BERLIN).strftime("%H:%M") == expected


def test_solar_noon_follows_daylight_saving_time() -> None:
    """Solar noon is about an hour later on the clock in summer."""
    point = SchedulePoint(5000, sun_event=NOON)
    winter = point_time(point, MONDAY, TZ, BERLIN)
    summer = point_time(point, SUMMER, TZ, BERLIN)
    assert winter.strftime("%H") == "11"
    assert summer.strftime("%H") == "13"


def test_changes_like_daylight() -> None:
    """From sunrise to noon it changes fast at first and slowly at the end."""
    points = [
        SchedulePoint(2700, sun_event=SUNRISE),
        SchedulePoint(5000, sun_event=NOON),
    ]
    sunrise = point_time(points[0], MONDAY, TZ, BERLIN)
    noon = point_time(points[1], MONDAY, TZ, BERLIN)
    quarter = (noon - sunrise) / 4
    values = [
        MIRED_OF(target_at(points, sunrise + quarter * index, BERLIN))
        for index in range(5)
    ]
    steps = [earlier - later for earlier, later in itertools.pairwise(values)]
    # Always towards the cold value, and less so each quarter.
    assert all(step > 0 for step in steps)
    assert steps == sorted(steps, reverse=True)


def test_eases_across_midnight() -> None:
    """Without the sun rising or setting throughout, it eases in and out."""
    points = [SchedulePoint(2000, dt.time(22, 0)), SchedulePoint(3000, dt.time(2, 0))]
    early = target_at(points, _at(22, 30), BERLIN).kelvin
    middle = target_at(points, _at(0, 0), BERLIN).kelvin
    # Slow at first: less than an eighth of the way after an eighth of the time.
    assert early < 2100
    # Halfway in time is halfway in mired.
    assert middle == 2400


def MIRED_OF(target: Target) -> float:
    return 1_000_000 / target.kelvin


def test_no_points() -> None:
    """Without points there is no target."""
    assert target_at([], _at(12, 0)) is None


@pytest.mark.parametrize("hour", range(24))
def test_default_schedule_in_range(hour: int) -> None:
    """The default schedule stays between its warmest and coldest point."""
    assert 2700 <= target_at(DEFAULT_SCHEDULE, _at(hour), BERLIN).kelvin <= 5000


def test_daylight_saving_change() -> None:
    """Progress follows real time on the night the clocks go back."""
    points = [SchedulePoint(2000, dt.time(1, 0)), SchedulePoint(3000, dt.time(4, 0))]
    # 25 October 2026: 01:00 to 04:00 are four real hours in Berlin.
    day = dt.date(2026, 10, 25)
    halfway = dt.datetime(2026, 10, 25, 1, 0, tzinfo=TZ).astimezone(dt.UTC) + (
        dt.timedelta(hours=2)
    )
    # 500 and 333.3 mired, halfway is 416.7 mired.
    assert target_at(points, halfway.astimezone(TZ)) == Target(2400)
    assert target_at(points, _at(4, 0, day)) == Target(3000)


def test_options_round_trip() -> None:
    """Points survive storing, and are stored sorted by time."""
    points = (
        SchedulePoint(3000, dt.time(18, 0)),
        SchedulePoint(5000, dt.time(7, 0), weekdays=frozenset({0, 1, 2, 3, 4})),
        *DEFAULT_SCHEDULE,
    )
    stored = points_to_options(points)
    assert [point.get("time") or point["anchor"] for point in stored] == [
        "sunrise",
        "07:00",
        "noon",
        "18:00",
        "sunset",
        "sunset",
    ]
    assert set(points_from_options(stored)) == set(points)
    assert points_from_options(None) == DEFAULT_SCHEDULE


def test_old_options_load() -> None:
    """Points stored before sun points existed are clock points."""
    assert points_from_options(
        [{"time": "07:00", "kelvin": 5000, "weekdays": [5, 6]}]
    ) == (SchedulePoint(5000, dt.time(7, 0), weekdays=frozenset({5, 6})),)


def test_format_when() -> None:
    """Sun points show their event, offset, window and today's time."""
    point = DEFAULT_SCHEDULE[3]
    assert format_when(point, "en") == "Sunset + 1:30 (until 22:00)"
    assert (
        format_when(point, "de", _at(18, 18))
        == "Sonnenuntergang + 1:30 (bis 22:00, heute 18:18)"
    )
    assert format_when(DEFAULT_SCHEDULE[0], "en") == f"Sunrise (06:00{DASH}07:30)"


@pytest.mark.parametrize(
    ("weekdays", "text"),
    [
        (None, "täglich"),
        (frozenset({0, 1, 2, 3, 4}), f"Mo{DASH}Fr"),
        (frozenset({5, 6}), "Sa, So"),
        (frozenset({0, 1, 2, 4, 6}), f"Mo{DASH}Mi, Fr, So"),
    ],
)
def test_format_weekdays(weekdays, text: str) -> None:
    """Weekdays are shown short, with ranges."""
    from custom_components.light_buddy.schedule import format_weekdays

    assert format_weekdays(weekdays, "de") == text


def _scan(points, day: dt.date, tz: ZoneInfo, sun: Sun) -> list[int]:
    """Return the target every 5 minutes from 18:00 to 06:00 the next day."""
    start = dt.datetime.combine(day, dt.time(18, 0), tzinfo=tz)
    return [
        target_at(points, start + dt.timedelta(minutes=minutes), sun).kelvin
        for minutes in range(0, 12 * 60 + 1, 5)
    ]


def test_late_sunset_keeps_the_latest_time() -> None:
    """Where the sun sets after 22:00, it is warm by 22:00 and stays warm."""
    oslo = ZoneInfo("Europe/Oslo")
    values = _scan(DEFAULT_SCHEDULE, SUMMER, oslo, Sun(59.91, 10.75))
    at_22 = values[(22 - 18) * 12]
    assert at_22 == 2700
    assert all(value == 2700 for value in values[(22 - 18) * 12 :])
    # Getting warmer only, towards the night.
    assert values[: (22 - 18) * 12] == sorted(values[: (22 - 18) * 12], reverse=True)


@pytest.mark.parametrize("day", [dt.date(2026, 6, 21), dt.date(2026, 12, 21)])
def test_polar_day_and_night(day: dt.date) -> None:
    """Without sunrise or sunset, they are at their typical times."""
    tromso = ZoneInfo("Europe/Oslo")
    sun = Sun(69.65, 18.96)
    midnight = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tromso)
    assert target_at(DEFAULT_SCHEDULE, midnight, sun) == Target(2700)
    noon = point_time(SchedulePoint(0, sun_event=NOON), day, tromso, sun)
    assert target_at(DEFAULT_SCHEDULE, noon, sun) == Target(5000)


def test_points_at_the_same_moment() -> None:
    """Of two points at the same moment the later one counts, without a jump."""
    points = [
        SchedulePoint(5000, dt.time(12, 0)),
        SchedulePoint(3000, dt.time(22, 0)),
        SchedulePoint(
            2700, sun_event=SUNSET, offset_minutes=360, latest=dt.time(22, 0)
        ),
    ]
    before = target_at(points, _at(21, 59), BERLIN).kelvin
    assert abs(before - 2700) < 20
    assert target_at(points, _at(22, 0), BERLIN) == Target(2700)


def test_unknown_anchor_is_skipped() -> None:
    """Points with an unknown anchor, e.g. edited by hand, are left out."""
    assert points_from_options(
        [
            {"anchor": "moonrise", "kelvin": 3000},
            {"anchor": "time", "time": "07:00", "kelvin": 5000},
        ]
    ) == (SchedulePoint(5000, dt.time(7, 0)),)
    assert points_from_options([{"anchor": "moonrise", "kelvin": 3000}]) == (
        DEFAULT_SCHEDULE
    )


def test_window_keeps_points_apart() -> None:
    """Pulled back by a window, a point stays a while before the next one."""
    oslo = ZoneInfo("Europe/Oslo")
    times = {
        point: when
        for when, point in day_times(
            DEFAULT_SCHEDULE, dt.date(2026, 5, 20), oslo, Sun(59.91, 10.75)
        )
    }
    sunset, later = times[DEFAULT_SCHEDULE[2]], times[DEFAULT_SCHEDULE[3]]
    assert later.strftime("%H:%M") == "22:00"
    assert later - sunset >= dt.timedelta(minutes=30)
