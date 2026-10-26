"""Tests for the schedule math."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.light_buddy.schedule import (
    DASH,
    DEFAULT_SCHEDULE,
    SchedulePoint,
    Target,
    target_at,
)

TZ = ZoneInfo("Europe/Berlin")
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
    """The default schedule is coldest at noon and holds the night value."""
    assert target_at(DEFAULT_SCHEDULE, _at(12, 30)) == Target(5000)
    assert target_at(DEFAULT_SCHEDULE, _at(23, 0)) == Target(2200)
    assert target_at(DEFAULT_SCHEDULE, _at(3, 0)) == Target(2200)


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


def test_brightness_between_points_that_have_one() -> None:
    """Points without brightness do not interrupt the brightness curve."""
    points = [
        SchedulePoint(3000, dt.time(6, 0), brightness=40),
        SchedulePoint(4000, dt.time(8, 0)),
        SchedulePoint(5000, dt.time(10, 0), brightness=100),
    ]
    assert target_at(points, _at(8, 0)) == Target(4000, 70)
    assert target_at(DEFAULT_SCHEDULE, _at(8, 0)).brightness is None


def test_sun_point() -> None:
    """Sun points use the day's sun time plus their offset."""

    def sun_time(event: str, day: dt.date) -> dt.datetime:
        assert event == "sunset"
        return dt.datetime.combine(day, dt.time(17, 0), tzinfo=TZ)

    points = [
        SchedulePoint(4000, dt.time(12, 0)),
        SchedulePoint(2700, sun_event="sunset", offset_minutes=-30),
    ]
    assert target_at(points, _at(16, 30), sun_time) == Target(2700)
    assert target_at(points, _at(16, 30)) == Target(4000)


def test_no_points() -> None:
    """Without points there is no target."""
    assert target_at([], _at(12, 0)) is None


@pytest.mark.parametrize("hour", range(24))
def test_default_schedule_in_range(hour: int) -> None:
    """The default schedule stays between its warmest and coldest point."""
    assert 2200 <= target_at(DEFAULT_SCHEDULE, _at(hour)).kelvin <= 5000


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


def test_brightness_on_some_days_only() -> None:
    """Brightness points on weekdays carry over the weekend."""
    weekdays = frozenset(range(5))
    points = [
        SchedulePoint(3000, dt.time(6, 0)),
        SchedulePoint(4000, dt.time(18, 0)),
        SchedulePoint(4000, dt.time(8, 0), brightness=100, weekdays=weekdays),
        SchedulePoint(4000, dt.time(20, 0), brightness=40, weekdays=weekdays),
    ]
    saturday = MONDAY + dt.timedelta(days=5)
    # Between Friday 20:00 and Monday 08:00.
    assert 40 < target_at(points, _at(12, 0, saturday)).brightness < 100


def test_options_round_trip() -> None:
    """Points survive storing, and are stored sorted by time."""
    from custom_components.light_buddy.schedule import (
        points_from_options,
        points_to_options,
    )

    points = (
        SchedulePoint(3000, dt.time(18, 0), brightness=40),
        SchedulePoint(5000, dt.time(7, 0), weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    stored = points_to_options(points)
    assert [point["time"] for point in stored] == ["07:00", "18:00"]
    assert set(points_from_options(stored)) == set(points)
    assert points_from_options(None) == DEFAULT_SCHEDULE


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
