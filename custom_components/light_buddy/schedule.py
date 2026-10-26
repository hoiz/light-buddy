"""Color temperature by time of day.

Pure functions without Home Assistant state, so they can be tested directly.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
from functools import cached_property
import itertools
import math
from typing import Any

import astral
import astral.sun

from .const import (
    ANCHOR_TIME,
    CONF_ANCHOR,
    CONF_EARLIEST,
    CONF_KELVIN,
    CONF_LATEST,
    CONF_SUN_OFFSET,
    CONF_TIME,
    CONF_WEEKDAYS,
)

# Interpolating mireds looks even, interpolating Kelvin would not.
MIRED = 1_000_000

# Far enough to find the previous and next point of any weekly schedule.
SEARCH_DAYS = 7

SUNRISE = "sunrise"
NOON = "noon"
SUNSET = "sunset"
SUN_EVENTS = (SUNRISE, NOON, SUNSET)
# Where sun points are sorted among clock points, before their offset.
NOMINAL_SUN_TIMES = {
    SUNRISE: dt.time(6, 0),
    NOON: dt.time(12, 0),
    SUNSET: dt.time(18, 0),
}

# Points along a segment checked for the sun rising or setting throughout.
MONOTONIC_CHECKS = 8


@dataclass(frozen=True, slots=True)
class SchedulePoint:
    """A color temperature at a time of day.

    The time is either a clock time, or a sun event like "sunset" shifted by
    some minutes, and kept between the earliest and latest clock time.
    Weekdays limit the point to some days, 0 is Monday.
    """

    kelvin: int
    time: dt.time | None = None
    sun_event: str | None = None
    offset_minutes: int = 0
    weekdays: frozenset[int] | None = None
    earliest: dt.time | None = None
    latest: dt.time | None = None


@dataclass(frozen=True, slots=True)
class Target:
    """What the lights of a room should show now."""

    kelvin: int


@dataclass(frozen=True)
class Sun:
    """The sun at a place on Earth."""

    latitude: float
    longitude: float
    elevation: float = 0

    @cached_property
    def _observer(self) -> astral.Observer:
        return astral.Observer(self.latitude, self.longitude, self.elevation)

    def time(self, event: str, day: dt.date, tzinfo: dt.tzinfo) -> dt.datetime | None:
        """Return when a sun event happens on a day, None if it does not."""
        try:
            return getattr(astral.sun, event)(self._observer, day, tzinfo=tzinfo)
        except ValueError:
            # Polar day or night.
            return None

    def height(self, when: dt.datetime) -> float:
        """Return the sine of the sun's elevation, which daylight follows."""
        return math.sin(math.radians(astral.sun.elevation(self._observer, when)))


# Follows the sun: coldest at solar noon, warm soon after sunset, and the
# morning starts with sunrise, within limits. Never warmer than 2700 K.
DEFAULT_SCHEDULE: tuple[SchedulePoint, ...] = (
    SchedulePoint(
        2700, sun_event=SUNRISE, earliest=dt.time(6, 0), latest=dt.time(7, 30)
    ),
    SchedulePoint(5000, sun_event=NOON),
    SchedulePoint(3200, sun_event=SUNSET),
    SchedulePoint(2700, sun_event=SUNSET, offset_minutes=90, latest=dt.time(22, 0)),
)


def target_at(
    points: Sequence[SchedulePoint],
    now: dt.datetime,
    sun: Sun | None = None,
) -> Target | None:
    """Return the target at a time, between the schedule's points.

    Between two points it changes like daylight does, as far as the sun
    rises or sets throughout, and eases in and out otherwise. Works across
    midnight and across days with different points. None without points.
    """
    # Most schedules have points every day, so a day around now is enough.
    for search_days in (1, SEARCH_DAYS):
        occurrences = occurrences_around(points, now, sun, search_days)
        mired = _interpolate(
            [(when, MIRED / point.kelvin) for when, point in occurrences], now, sun
        )
        if mired is not None:
            break
    else:
        return None
    return Target(round(MIRED / mired))


def point_time(
    point: SchedulePoint, day: dt.date, tzinfo: dt.tzinfo, sun: Sun | None
) -> dt.datetime | None:
    """Return when a point happens on a day, ignoring its weekdays.

    None for sun points without the sun. Without the event that day, e.g.
    sunset in polar night, the point is at the event's typical time.
    """
    if point.time is not None:
        return dt.datetime.combine(day, point.time, tzinfo=tzinfo)
    if point.sun_event is None or sun is None:
        return None
    if (event := sun.time(point.sun_event, day, tzinfo)) is None:
        # Polar day or night: at its typical time instead.
        event = dt.datetime.combine(
            day, NOMINAL_SUN_TIMES.get(point.sun_event, dt.time(12, 0)), tzinfo=tzinfo
        )
    when = event.astimezone(tzinfo) + dt.timedelta(minutes=point.offset_minutes)
    if point.earliest is not None:
        when = max(when, dt.datetime.combine(day, point.earliest, tzinfo=tzinfo))
    if point.latest is not None:
        when = min(when, dt.datetime.combine(day, point.latest, tzinfo=tzinfo))
    return when


def occurrences_around(
    points: Sequence[SchedulePoint],
    now: dt.datetime,
    sun: Sun | None,
    search_days: int,
) -> list[tuple[dt.datetime, SchedulePoint]]:
    """Return when the points happen in the days around now, sorted, in UTC.

    In UTC, time differences stay right on days the clocks change.
    """
    assert now.tzinfo is not None
    rank = {point: index for index, point in enumerate(sort_points(points))}
    ranked = []
    for day_offset in range(-search_days, search_days + 1):
        day = now.date() + dt.timedelta(days=day_offset)
        ranked.extend(
            (when.astimezone(dt.UTC), rank[point], point)
            for when, point in day_times(points, day, now.tzinfo, sun)
        )
    ranked.sort(key=lambda occurrence: occurrence[:2])
    # Of points at the same moment, the last one counts, so the curve does
    # not jump there.
    return [
        (when, point)
        for index, (when, _, point) in enumerate(ranked)
        if index + 1 == len(ranked) or ranked[index + 1][0] != when
    ]


def day_times(
    points: Sequence[SchedulePoint],
    day: dt.date,
    tzinfo: dt.tzinfo,
    sun: Sun | None,
) -> list[tuple[dt.datetime, SchedulePoint]]:
    """Return when the points of a day happen, in no particular order.

    Points of the same sun event keep the order of their offsets, also
    when a window moves one of them: e.g. "sunset + 1:30, at the latest
    22:00" pulls sunset itself back to 22:00 where the sun sets later.
    """
    times: dict[SchedulePoint, dt.datetime] = {}
    for point in points:
        if point.weekdays is not None and day.weekday() not in point.weekdays:
            continue
        if (when := point_time(point, day, tzinfo, sun)) is not None:
            times[point] = when
    for event in SUN_EVENTS:
        same_event = sorted(
            (point for point in times if point.sun_event == event),
            key=lambda point: point.offset_minutes,
        )
        # From the largest offset back, no point after the next one.
        for following, point in itertools.pairwise(reversed(same_event)):
            times[point] = min(times[point], times[following])
    return [(when, point) for point, when in times.items()]


def _interpolate(
    values: Sequence[tuple[dt.datetime, float]], now: dt.datetime, sun: Sun | None
) -> float | None:
    """Return the value at now between the surrounding values.

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
    if start_value == end_value:
        return start_value
    return start_value + _progress(start, end, now, sun) * (end_value - start_value)


def _progress(
    start: dt.datetime, end: dt.datetime, now: dt.datetime, sun: Sun | None
) -> float:
    """Return how far now is from start to end, between 0 and 1.

    Like daylight, if the sun rises or sets all the way: fast after sunrise
    and before sunset, slow around noon. Otherwise, e.g. across midnight, it
    eases in and out over time.
    """
    share = (now - start) / (end - start)
    if sun is not None:
        heights = [
            sun.height(start + (end - start) * index / MONOTONIC_CHECKS)
            for index in range(MONOTONIC_CHECKS + 1)
        ]
        steps = [later - earlier for earlier, later in itertools.pairwise(heights)]
        if all(step > 0 for step in steps) or all(step < 0 for step in steps):
            progress = (sun.height(now) - heights[0]) / (heights[-1] - heights[0])
            return min(max(progress, 0.0), 1.0)
    return (1 - math.cos(math.pi * share)) / 2


def _parse_time(value: str | None) -> dt.time | None:
    return dt.time.fromisoformat(value) if value else None


def points_from_options(
    data: Sequence[Mapping[str, Any]] | None,
) -> tuple[SchedulePoint, ...]:
    """Return the schedule stored in the options, or the default one.

    Points stored before sun points existed have only a time.
    """
    if not data:
        return DEFAULT_SCHEDULE
    points: list[SchedulePoint] = []
    for point in data:
        anchor = point.get(CONF_ANCHOR, ANCHOR_TIME)
        if anchor != ANCHOR_TIME and anchor not in SUN_EVENTS:
            # Unknown, e.g. edited by hand.
            continue
        weekdays = frozenset(point[CONF_WEEKDAYS]) if point.get(CONF_WEEKDAYS) else None
        if anchor == ANCHOR_TIME:
            points.append(
                SchedulePoint(
                    kelvin=int(point[CONF_KELVIN]),
                    time=dt.time.fromisoformat(point[CONF_TIME]),
                    weekdays=weekdays,
                )
            )
        else:
            points.append(
                SchedulePoint(
                    kelvin=int(point[CONF_KELVIN]),
                    sun_event=anchor,
                    offset_minutes=int(point.get(CONF_SUN_OFFSET, 0)),
                    weekdays=weekdays,
                    earliest=_parse_time(point.get(CONF_EARLIEST)),
                    latest=_parse_time(point.get(CONF_LATEST)),
                )
            )
    return tuple(points) or DEFAULT_SCHEDULE


def _format_time(value: dt.time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def points_to_options(points: Sequence[SchedulePoint]) -> list[dict[str, Any]]:
    """Return a schedule for storing, sorted by time."""
    stored = []
    for point in sort_points(points):
        data: dict[str, Any] = {
            CONF_KELVIN: point.kelvin,
            CONF_WEEKDAYS: sorted(point.weekdays) if point.weekdays else [],
        }
        if point.time is not None:
            data[CONF_ANCHOR] = ANCHOR_TIME
            data[CONF_TIME] = _format_time(point.time)
        elif point.sun_event is not None:
            data[CONF_ANCHOR] = point.sun_event
            data[CONF_SUN_OFFSET] = point.offset_minutes
            data[CONF_EARLIEST] = _format_time(point.earliest)
            data[CONF_LATEST] = _format_time(point.latest)
        else:
            continue
        stored.append(data)
    return stored


def nominal_time(point: SchedulePoint) -> dt.time:
    """Return a typical time of a point, to sort points by."""
    if point.time is not None:
        return point.time
    base = NOMINAL_SUN_TIMES.get(point.sun_event or "", dt.time())
    minutes = min(
        max(base.hour * 60 + base.minute + point.offset_minutes, 0), 24 * 60 - 1
    )
    when = dt.time(minutes // 60, minutes % 60)
    if point.earliest is not None:
        when = max(when, point.earliest)
    if point.latest is not None:
        when = min(when, point.latest)
    return when


def sort_points(
    points: Sequence[SchedulePoint],
    today: dt.datetime | None = None,
    sun: Sun | None = None,
) -> list[SchedulePoint]:
    """Return points by their time today, or their typical time.

    Points for every day first.
    """

    def when(point: SchedulePoint) -> dt.time:
        if (
            today is not None
            and today.tzinfo is not None
            and (moment := point_time(point, today.date(), today.tzinfo, sun))
            is not None
        ):
            return moment.time()
        return nominal_time(point)

    return sorted(
        points,
        key=lambda point: (
            when(point),
            sorted(point.weekdays) if point.weekdays else [],
        ),
    )


def same_slot(point: SchedulePoint, other: SchedulePoint) -> bool:
    """Return True if two points are at the same time on a common day.

    Sun points are at the same time with the same event and offset.
    """
    if (point.time, point.sun_event) != (other.time, other.sun_event):
        return False
    if point.sun_event is not None and point.offset_minutes != other.offset_minutes:
        return False
    days = point.weekdays or ALL_WEEKDAYS
    other_days = other.weekdays or ALL_WEEKDAYS
    return not days.isdisjoint(other_days)


ALL_WEEKDAYS = frozenset(range(7))
# For ranges of days.
DASH = "\u2013"
MINUS = "\u2212"

WEEKDAY_NAMES = {
    "de": ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"),
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
}
EVERY_DAY = {"de": "täglich", "en": "every day"}
SUN_EVENT_NAMES = {
    "de": {SUNRISE: "Sonnenaufgang", NOON: "Mittag", SUNSET: "Sonnenuntergang"},
    "en": {SUNRISE: "Sunrise", NOON: "Solar noon", SUNSET: "Sunset"},
}
WINDOW_WORDS = {
    "de": ("ab", "bis", "heute"),
    "en": ("from", "until", "today"),
}


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


def format_when(
    point: SchedulePoint,
    language: str,
    today: dt.datetime | None = None,
    sun: Sun | None = None,
) -> str:
    """Return when a point is, e.g. "Sunset + 1:30 (until 22:00, today 18:25)"."""
    if point.time is not None:
        return point.time.strftime("%H:%M")
    names = SUN_EVENT_NAMES.get(language, SUN_EVENT_NAMES["en"])
    since, until, today_word = WINDOW_WORDS.get(language, WINDOW_WORDS["en"])
    text = names.get(point.sun_event or "", point.sun_event or "")
    if offset := point.offset_minutes:
        sign = "+" if offset > 0 else MINUS
        text += f" {sign} {abs(offset) // 60}:{abs(offset) % 60:02d}"
    details = []
    if point.earliest is not None and point.latest is not None:
        details.append(f"{point.earliest:%H:%M}{DASH}{point.latest:%H:%M}")
    elif point.earliest is not None:
        details.append(f"{since} {point.earliest:%H:%M}")
    elif point.latest is not None:
        details.append(f"{until} {point.latest:%H:%M}")
    if (
        today is not None
        and today.tzinfo is not None
        and (when := point_time(point, today.date(), today.tzinfo, sun)) is not None
    ):
        details.append(f"{today_word} {when:%H:%M}")
    if details:
        text += f" ({', '.join(details)})"
    return text


def point_columns(
    point: SchedulePoint,
    language: str,
    today: dt.datetime | None = None,
    sun: Sun | None = None,
) -> tuple[str, str, str]:
    """Return a point's time, color temperature and days as text."""
    return (
        format_when(point, language, today, sun),
        f"{point.kelvin} K",
        format_weekdays(point.weekdays, language),
    )


def format_point(
    point: SchedulePoint,
    language: str,
    today: dt.datetime | None = None,
    sun: Sun | None = None,
) -> str:
    """Return a point as one line, e.g. "06:30 · 3000 K · Mo-Fr"."""
    return " · ".join(point_columns(point, language, today, sun))


TABLE_HEADERS = {
    "de": ("Zeit", "Farbtemperatur", "Tage"),
    "en": ("Time", "Color temperature", "Days"),
}


def format_table(
    points: Sequence[SchedulePoint],
    language: str,
    today: dt.datetime | None = None,
    sun: Sun | None = None,
) -> str:
    """Return points as a Markdown table, in their order, for the options flow."""
    header = TABLE_HEADERS.get(language, TABLE_HEADERS["en"])
    rows = [f"| {' | '.join(header)} |", "|---|---|---|"]
    rows.extend(
        f"| {' | '.join(point_columns(point, language, today, sun))} |"
        for point in points
    )
    return "\n".join(rows)
