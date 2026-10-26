"""Color temperature and brightness by time of day.

Pure functions without Home Assistant state, so they can be tested directly.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
from typing import Any

from .const import CONF_BRIGHTNESS, CONF_KELVIN, CONF_TIME, CONF_WEEKDAYS

# Interpolating mireds looks even, interpolating Kelvin would not.
MIRED = 1_000_000

# Far enough to find the previous and next point of any weekly schedule.
SEARCH_DAYS = 7


@dataclass(frozen=True, slots=True)
class SchedulePoint:
    """A color temperature, and optionally a brightness, at a time of day.

    The time is either a clock time, or a sun event like "sunset" shifted by
    some minutes. Weekdays limit the point to some days, 0 is Monday.
    """

    kelvin: int
    time: dt.time | None = None
    sun_event: str | None = None
    offset_minutes: int = 0
    brightness: int | None = None
    weekdays: frozenset[int] | None = None


@dataclass(frozen=True, slots=True)
class Target:
    """What the lights of a room should show now."""

    kelvin: int
    # In percent, None leaves the brightness alone.
    brightness: int | None = None


# Follows daylight, coldest around noon, and does not start the morning as
# warm as natural dawn. Holds the night value between 22:30 and 05:30.
DEFAULT_SCHEDULE: tuple[SchedulePoint, ...] = (
    SchedulePoint(2200, dt.time(5, 30)),
    SchedulePoint(3000, dt.time(6, 30)),
    SchedulePoint(4000, dt.time(8, 0)),
    SchedulePoint(5000, dt.time(12, 30)),
    SchedulePoint(4500, dt.time(16, 0)),
    SchedulePoint(3500, dt.time(18, 30)),
    SchedulePoint(2700, dt.time(21, 0)),
    SchedulePoint(2200, dt.time(22, 30)),
)

type SunTime = Callable[[str, dt.date], dt.datetime | None]


def target_at(
    points: Sequence[SchedulePoint],
    now: dt.datetime,
    sun_time: SunTime | None = None,
) -> Target | None:
    """Return the target at a time, interpolated between the schedule's points.

    Works across midnight and across days with different points. Brightness
    is only interpolated between points that have one. None without points.
    """
    # Most schedules have points every day, so a day around now is enough.
    for search_days in (1, SEARCH_DAYS):
        occurrences = _occurrences(points, now, sun_time, search_days)
        kelvin = _interpolate(
            [(when, MIRED / point.kelvin) for when, point in occurrences], now
        )
        if kelvin is not None:
            break
    else:
        return None
    brightness = _interpolate_brightness(occurrences, now)
    if brightness is None and search_days < SEARCH_DAYS:
        # Brightness may come from fewer points than color temperature.
        brightness = _interpolate_brightness(
            _occurrences(points, now, sun_time, SEARCH_DAYS), now
        )
    return Target(
        round(MIRED / kelvin), None if brightness is None else round(brightness)
    )


def _occurrences(
    points: Sequence[SchedulePoint],
    now: dt.datetime,
    sun_time: SunTime | None,
    search_days: int,
) -> list[tuple[dt.datetime, SchedulePoint]]:
    """Return when the points happen in the days around now, sorted, in UTC.

    In UTC, time differences stay right on days the clocks change.
    """
    occurrences = []
    for day_offset in range(-search_days, search_days + 1):
        day = now.date() + dt.timedelta(days=day_offset)
        for point in points:
            if point.weekdays is not None and day.weekday() not in point.weekdays:
                continue
            if point.time is not None:
                when = dt.datetime.combine(day, point.time, tzinfo=now.tzinfo)
            elif point.sun_event is not None and sun_time is not None:
                if (sun := sun_time(point.sun_event, day)) is None:
                    # Polar day or night: no such event that day.
                    continue
                when = sun.astimezone(now.tzinfo) + dt.timedelta(
                    minutes=point.offset_minutes
                )
            else:
                continue
            occurrences.append((when.astimezone(dt.UTC), point))
    occurrences.sort(key=lambda occurrence: occurrence[0])
    return occurrences


def _interpolate_brightness(
    occurrences: Sequence[tuple[dt.datetime, SchedulePoint]], now: dt.datetime
) -> float | None:
    return _interpolate(
        [
            (when, point.brightness)
            for when, point in occurrences
            if point.brightness is not None
        ],
        now,
    )


def _interpolate(
    values: Sequence[tuple[dt.datetime, float]], now: dt.datetime
) -> float | None:
    """Return the linear interpolation at now between the surrounding values.

    None unless there are values before and after now.
    """
    now = now.astimezone(dt.UTC)
    previous = next_ = None
    for when, value in values:
        if when <= now:
            previous = (when, value)
        else:
            next_ = (when, value)
            break
    if previous is None or next_ is None:
        return None
    (start, start_value), (end, end_value) = previous, next_
    progress = (now - start) / (end - start)
    return start_value + progress * (end_value - start_value)


def points_from_options(
    data: Sequence[Mapping[str, Any]] | None,
) -> tuple[SchedulePoint, ...]:
    """Return the schedule stored in the options, or the default one."""
    if not data:
        return DEFAULT_SCHEDULE
    return tuple(
        SchedulePoint(
            kelvin=int(point[CONF_KELVIN]),
            time=dt.time.fromisoformat(point[CONF_TIME]),
            brightness=point.get(CONF_BRIGHTNESS),
            weekdays=frozenset(point[CONF_WEEKDAYS])
            if point.get(CONF_WEEKDAYS)
            else None,
        )
        for point in data
    )


def points_to_options(points: Sequence[SchedulePoint]) -> list[dict[str, Any]]:
    """Return a schedule of clock time points for storing, sorted by time."""
    return [
        {
            CONF_TIME: point.time.strftime("%H:%M"),
            CONF_KELVIN: point.kelvin,
            CONF_BRIGHTNESS: point.brightness,
            CONF_WEEKDAYS: sorted(point.weekdays) if point.weekdays else [],
        }
        for point in sort_points(points)
        if point.time is not None
    ]


def sort_points(points: Sequence[SchedulePoint]) -> list[SchedulePoint]:
    """Return points by time, points for every day first."""
    return sorted(
        points,
        key=lambda point: (
            point.time or dt.time.min,
            sorted(point.weekdays) if point.weekdays else [],
        ),
    )


def same_slot(point: SchedulePoint, other: SchedulePoint) -> bool:
    """Return True if two points are at the same time on a common day."""
    if point.time != other.time:
        return False
    days = point.weekdays or ALL_WEEKDAYS
    other_days = other.weekdays or ALL_WEEKDAYS
    return not days.isdisjoint(other_days)


ALL_WEEKDAYS = frozenset(range(7))
# For ranges of days, and for no brightness.
DASH = "\u2013"

WEEKDAY_NAMES = {
    "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}
EVERY_DAY = {"de": "täglich", "en": "every day"}


def format_weekdays(weekdays: frozenset[int] | None, language: str) -> str:
    """Return weekdays as short text, with ranges, e.g. "Mo-Fr, So"."""
    names = WEEKDAY_NAMES.get(language, WEEKDAY_NAMES["en"])
    if not weekdays or weekdays == ALL_WEEKDAYS:
        return EVERY_DAY.get(language, EVERY_DAY["en"])
    days = sorted(weekdays)
    parts = []
    start = previous = days[0]
    for day in [*days[1:], None]:
        if day is not None and day == previous + 1:
            previous = day
            continue
        if previous - start >= 2:
            parts.append(f"{names[start]}{DASH}{names[previous]}")
        else:
            parts.extend(names[number] for number in range(start, previous + 1))
        if day is not None:
            start = previous = day
    return ", ".join(parts)


def point_columns(point: SchedulePoint, language: str) -> tuple[str, str, str, str]:
    """Return a point's time, color temperature, brightness and days as text."""
    return (
        point.time.strftime("%H:%M") if point.time else "",
        f"{point.kelvin} K",
        DASH if point.brightness is None else f"{point.brightness} %",
        format_weekdays(point.weekdays, language),
    )


def format_point(point: SchedulePoint, language: str) -> str:
    """Return a point as one line, e.g. "06:30 · 3000 K · 40 % · Mo-Fr"."""
    columns = point_columns(point, language)
    # Without brightness, the line leaves it out instead of showing a dash.
    return " · ".join(column for column in columns if column != DASH)


TABLE_HEADERS = {
    "de": ("Uhrzeit", "Farbtemperatur", "Helligkeit", "Tage"),
    "en": ("Time", "Color temperature", "Brightness", "Days"),
}


def format_table(points: Sequence[SchedulePoint], language: str) -> str:
    """Return points as a Markdown table, in their order, for the options flow."""
    header = TABLE_HEADERS.get(language, TABLE_HEADERS["en"])
    rows = [f"| {' | '.join(header)} |", "|---|---|---|---|"]
    rows.extend(f"| {' | '.join(point_columns(point, language))} |" for point in points)
    return "\n".join(rows)
