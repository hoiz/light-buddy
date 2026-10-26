"""A chart of the schedule, shown in the options flow.

The flow embeds it as an image whose URL carries the points, so changes
that are not saved yet show, too.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
import datetime as dt
from functools import lru_cache
from http import HTTPStatus
import re
from xml.sax.saxutils import escape

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS
from homeassistant.util import dt as dt_util

from .automation import hass_sun
from .schedule import (
    MIRED,
    NOON,
    SUNRISE,
    SUNSET,
    SchedulePoint,
    Sun,
    day_times,
    format_weekdays,
    target_at,
)

URL = "/api/light_buddy/curve.svg"
RENDER_LOCK = asyncio.Lock()
MAX_POINTS = 50
MIN_KELVIN = 1000
MAX_KELVIN = 10000
MAX_OFFSET = 24 * 60

SAMPLE_MINUTES = 10
# Days of the year shown next to today, in the northern hemisphere's order.
SOLSTICES = ((6, 21), (12, 21))

WIDTH = 720
HEIGHT = 260
LEFT, RIGHT, TOP, BOTTOM = 60, 16, 16, 36
# Readable on light and dark backgrounds.
GREY = "#8a8a8a"
# Kelvin values the axis is labeled with, unevenly apart on its mired scale.
TICKS = (1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 6500, 8000, 10000)
MIN_LABEL_GAP = 14
# The ends of the color scale of the line.
WARM_KELVIN = 2000
COLD_KELVIN = 6500
# Tells the lines of different days apart.
DASHES = ("", "8 5", "2 4", "12 4 2 4")
SEASON_NAMES = {
    "de": ("Längster Tag", "Kürzester Tag"),
    "en": ("Longest day", "Shortest day"),
}

ANCHOR_CODES = {SUNRISE: "r", NOON: "n", SUNSET: "s"}
ANCHOR_BY_CODE = {code: anchor for anchor, code in ANCHOR_CODES.items()}
CLOCK = re.compile(r"(\d{2})(\d{2})")
SUN = re.compile(r"([rns])([+-]\d{1,4})")
WINDOW = re.compile(r"w((?:\d{4})?)-((?:\d{4})?)")
KELVIN = re.compile(r"k(\d{4,5})")
DAYS = re.compile(r"d([0-6]{1,7})")

type Samples = tuple[tuple[int, int], ...]


def _clock(text: str) -> dt.time:
    """Return a time of four digits, raising ValueError if invalid."""
    if not (match := CLOCK.fullmatch(text)):
        raise ValueError(f"invalid time {text!r}")
    return dt.time(int(match[1]), int(match[2]))


def encode_points(points: Sequence[SchedulePoint]) -> str:
    """Return points as a short text for a URL.

    E.g. "0630_k3000_d56" for 06:30 on weekends, "s+90_w-2200_k2700" for
    90 minutes after sunset, until 22:00 at the latest.
    """
    parts = []
    for point in points:
        if point.time is not None:
            tokens = [f"{point.time:%H%M}"]
        elif point.sun_event in ANCHOR_CODES:
            tokens = [f"{ANCHOR_CODES[point.sun_event]}{point.offset_minutes:+d}"]
            if point.earliest is not None or point.latest is not None:
                earliest = f"{point.earliest:%H%M}" if point.earliest else ""
                latest = f"{point.latest:%H%M}" if point.latest else ""
                tokens.append(f"w{earliest}-{latest}")
        else:
            continue
        tokens.append(f"k{point.kelvin}")
        if point.weekdays:
            tokens.append("d" + "".join(str(day) for day in sorted(point.weekdays)))
        parts.append("_".join(tokens))
    return ",".join(parts)


def decode_points(text: str) -> tuple[SchedulePoint, ...]:
    """Return the points of encode_points, raising ValueError if invalid."""
    parts = text.split(",") if text else []
    if not parts or len(parts) > MAX_POINTS:
        raise ValueError("between 1 and 50 points")
    return tuple(_decode_point(part) for part in parts)


def _decode_point(part: str) -> SchedulePoint:
    tokens = part.split("_")
    anchor, rest = tokens[0], tokens[1:]
    window = rest.pop(0) if rest and rest[0].startswith("w") else None
    kelvin_token = rest.pop(0) if rest else ""
    days_token = rest.pop(0) if rest else None
    if rest or not (kelvin_match := KELVIN.fullmatch(kelvin_token)):
        raise ValueError(f"invalid point {part!r}")
    kelvin = int(kelvin_match[1])
    if not MIN_KELVIN <= kelvin <= MAX_KELVIN:
        raise ValueError(f"invalid color temperature {kelvin}")
    weekdays = None
    if days_token is not None:
        if not (days_match := DAYS.fullmatch(days_token)):
            raise ValueError(f"invalid weekdays {days_token!r}")
        weekdays = frozenset(int(day) for day in days_match[1])
    if sun_match := SUN.fullmatch(anchor):
        offset = int(sun_match[2])
        if abs(offset) > MAX_OFFSET:
            raise ValueError(f"invalid offset {offset}")
        earliest = latest = None
        if window is not None:
            if not (window_match := WINDOW.fullmatch(window)):
                raise ValueError(f"invalid window {window!r}")
            earliest = _clock(window_match[1]) if window_match[1] else None
            latest = _clock(window_match[2]) if window_match[2] else None
        return SchedulePoint(
            kelvin,
            sun_event=ANCHOR_BY_CODE[sun_match[1]],
            offset_minutes=offset,
            weekdays=weekdays,
            earliest=earliest,
            latest=latest,
        )
    if window is not None:
        raise ValueError(f"window of a clock time {part!r}")
    return SchedulePoint(kelvin, _clock(anchor), weekdays=weekdays)


def _wall_minute(when: dt.datetime, day: dt.date) -> int:
    """Return the minutes of a time since the start of a day, on the clock."""
    return (when.date() - day).days * 24 * 60 + when.hour * 60 + when.minute


def day_samples(
    points: Sequence[SchedulePoint],
    day: dt.date,
    tzinfo: dt.tzinfo,
    sun: Sun | None,
) -> Samples:
    """Return the target every few minutes of a day, and at each point.

    By the clock, so the line passes through the points.
    """
    minutes = set(range(0, 24 * 60 + 1, SAMPLE_MINUTES))
    minutes.update(
        _wall_minute(when, day) for when, _ in day_times(points, day, tzinfo, sun)
    )
    start = dt.datetime.combine(day, dt.time(), tzinfo=tzinfo)
    samples = []
    for minute in sorted(minute for minute in minutes if 0 <= minute <= 24 * 60):
        target = target_at(points, start + dt.timedelta(minutes=minute), sun)
        if target is None:
            return ()
        samples.append((minute, target.kelvin))
    return tuple(samples)


@lru_cache(maxsize=32)
def curves(
    points: tuple[SchedulePoint, ...],
    today: dt.date,
    tzinfo: dt.tzinfo,
    sun: Sun | None,
) -> tuple[list[tuple[frozenset[int], dt.date, Samples]], list[tuple[int, Samples]]]:
    """Return the curves to draw.

    One per group of weekdays with the same points, on its next day from
    today on. And for today's points, on the longest and shortest day, by
    their index in the season names. Cached, as the flow asks for the same
    chart again and again.
    """
    groups: dict[frozenset[int], set[int]] = {}
    for weekday in range(7):
        applying = frozenset(
            index
            for index, point in enumerate(points)
            if point.weekdays is None or weekday in point.weekdays
        )
        groups.setdefault(applying, set()).add(weekday)
    day_curves = []
    for days in groups.values():
        day = next(
            today + dt.timedelta(days=ahead)
            for ahead in range(7)
            if (today + dt.timedelta(days=ahead)).weekday() in days
        )
        day_curves.append((frozenset(days), day, day_samples(points, day, tzinfo, sun)))
    # Today's curve first.
    day_curves.sort(key=lambda curve: curve[1])

    season_curves: list[tuple[int, Samples]] = []
    if sun is not None and day_curves[0][2]:
        today_samples = day_curves[0][2]
        weekday = today.weekday()
        every_day = [
            replace(point, weekdays=None)
            for point in points
            if point.weekdays is None or weekday in point.weekdays
        ]
        solstices = [dt.date(today.year, month, day) for month, day in SOLSTICES]
        lengths = [_day_length(sun, day, tzinfo) for day in solstices]
        if None not in lengths:
            # In the southern hemisphere, December has the longest day.
            if lengths[1] > lengths[0]:  # type: ignore[operator]
                solstices.reverse()
            for index, day in enumerate(solstices):
                samples = day_samples(every_day, day, tzinfo, sun)
                if samples and samples != today_samples:
                    season_curves.append((index, samples))
    return day_curves, season_curves


def _day_length(sun: Sun, day: dt.date, tzinfo: dt.tzinfo) -> dt.timedelta | None:
    sunrise = sun.time(SUNRISE, day, tzinfo)
    sunset = sun.time(SUNSET, day, tzinfo)
    return sunset - sunrise if sunrise and sunset else None


def _color(kelvin: float) -> str:
    """Return a color from warm orange to cold blue, evenly in mired.

    The real color of cold white light is almost white, which would not show
    on a light background.
    """
    warm, cold = (255, 138, 0), (64, 156, 255)
    progress = (MIRED / WARM_KELVIN - MIRED / kelvin) / (
        MIRED / WARM_KELVIN - MIRED / COLD_KELVIN
    )
    progress = min(max(progress, 0), 1)
    red, green, blue = (
        round(start + progress * (end - start))
        for start, end in zip(warm, cold, strict=True)
    )
    return f"#{red:02x}{green:02x}{blue:02x}"


def render_svg(
    points: Sequence[SchedulePoint],
    now: dt.datetime,
    language: str,
    sun: Sun | None = None,
) -> str:
    """Return the schedule as an SVG chart of a day."""
    assert now.tzinfo is not None
    day_curves, season_curves = curves(tuple(points), now.date(), now.tzinfo, sun)
    all_kelvin = [
        kelvin
        for samples in [
            *(samples for _, _, samples in day_curves),
            *(samples for _, samples in season_curves),
        ]
        for _, kelvin in samples
    ]
    all_kelvin += [point.kelvin for point in points] or [2000, 6500]
    # The axis ends on ticks around the curve.
    low = max((tick for tick in TICKS if tick <= min(all_kelvin)), default=TICKS[0])
    high = min((tick for tick in TICKS if tick >= max(all_kelvin)), default=TICKS[-1])
    if high == low:
        index = TICKS.index(low)
        low, high = TICKS[max(index - 1, 0)], TICKS[min(index + 1, len(TICKS) - 1)]
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM

    def x(minute: float) -> float:
        return LEFT + minute / (24 * 60) * plot_width

    # Even in mired, as the eye sees color temperature and the schedule
    # interpolates it.
    def y(kelvin: float) -> float:
        return (
            TOP
            + (MIRED / kelvin - MIRED / high)
            / (MIRED / low - MIRED / high)
            * plot_height
        )

    def kelvin_at(fraction: float) -> float:
        """Return the color temperature a fraction down from the top."""
        return MIRED / (MIRED / high + fraction * (MIRED / low - MIRED / high))

    def polyline(samples: Samples, attributes: str) -> str:
        coordinates = " ".join(
            f"{x(minute):.1f},{y(kelvin):.1f}" for minute, kelvin in samples
        )
        return (
            f'<polyline points="{coordinates}" fill="none" stroke="url(#kelvin)" '
            f'stroke-linejoin="round" {attributes}/>'
        )

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'font-family="sans-serif" font-size="12" fill="{GREY}">'
    ]
    # Axes, every 3 hours and at round color temperatures.
    for hour in range(0, 25, 3):
        parts.append(
            f'<line x1="{x(hour * 60):.1f}" y1="{TOP}" x2="{x(hour * 60):.1f}" '
            f'y2="{TOP + plot_height}" stroke="{GREY}" stroke-opacity="0.25"/>'
            f'<text x="{x(hour * 60):.1f}" y="{HEIGHT - 14}" '
            f'text-anchor="middle">{hour:02d}:00</text>'
        )
    # Labels too close to the one below are left out, at the cold end.
    last_label = HEIGHT
    for kelvin in (tick for tick in TICKS if low <= tick <= high):
        if last_label - y(kelvin) < MIN_LABEL_GAP and kelvin != high:
            continue
        last_label = y(kelvin)
        parts.append(
            f'<line x1="{LEFT}" y1="{y(kelvin):.1f}" x2="{WIDTH - RIGHT}" '
            f'y2="{y(kelvin):.1f}" stroke="{GREY}" stroke-opacity="0.25"/>'
            f'<text x="{LEFT - 6}" y="{y(kelvin) + 4:.1f}" '
            f'text-anchor="end">{kelvin} K</text>'
        )
    # Now, in the time zone of Home Assistant.
    minute_now = now.hour * 60 + now.minute
    parts.append(
        f'<line x1="{x(minute_now):.1f}" y1="{TOP}" x2="{x(minute_now):.1f}" '
        f'y2="{TOP + plot_height}" stroke="{GREY}" stroke-dasharray="3 3"/>'
    )
    # The color depends on the color temperature only, so one gradient from
    # top to bottom colors all curves.
    parts.append(
        f'<defs><linearGradient id="kelvin" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="{y(high):.1f}" x2="0" y2="{y(low):.1f}">'
        + "".join(
            f'<stop offset="{index / 10:.1f}" '
            f'stop-color="{_color(kelvin_at(index / 10))}"/>'
            for index in range(11)
        )
        + "</linearGradient></defs>"
    )
    legend: list[tuple[str, str]] = []
    season_names = SEASON_NAMES.get(language, SEASON_NAMES["en"])
    # Thin, behind today's curve.
    for index, samples in season_curves:
        dash = "1 3" if index else ""
        dash_attr = f'stroke-dasharray="{dash}" ' if dash else ""
        parts.append(
            polyline(samples, f'stroke-width="1.5" stroke-opacity="0.6" {dash_attr}')
        )
        legend.append((season_names[index], f'stroke-width="1.5" {dash_attr}'))
    for index, (days, day, samples) in enumerate(day_curves):
        dash = DASHES[index % len(DASHES)]
        dash_attr = f'stroke-dasharray="{dash}" ' if dash else ""
        parts.append(polyline(samples, f'stroke-width="3" {dash_attr}'))
        for when, point in day_times(points, day, now.tzinfo, sun):
            parts.append(
                f'<circle cx="{x(_wall_minute(when, day)):.1f}" '
                f'cy="{y(point.kelvin):.1f}" r="4" '
                f'fill="{_color(point.kelvin)}" stroke="{GREY}"/>'
            )
        if len(day_curves) > 1:
            legend.append(
                (format_weekdays(days, language), f'stroke-width="3" {dash_attr}')
            )
    for index, (label, attributes) in enumerate(legend):
        label_y = TOP + 14 + index * 16
        parts.append(
            f'<line x1="{WIDTH - RIGHT - 150}" y1="{label_y - 4}" '
            f'x2="{WIDTH - RIGHT - 122}" y2="{label_y - 4}" stroke="{GREY}" '
            f"{attributes}/>"
            f'<text x="{WIDTH - RIGHT - 116}" y="{label_y}">{escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


class CurveChartView(HomeAssistantView):
    """Serve the chart of the points in the URL.

    Without authentication, as images in the options flow are loaded without
    a token. It only draws the points it is given.
    """

    url = URL
    name = "api:light_buddy:curve"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the chart."""
        try:
            points = decode_points(request.query.get("points", ""))
        except ValueError:
            return web.Response(status=HTTPStatus.BAD_REQUEST, text="Invalid points")
        hass = request.app[KEY_HASS]
        language = request.query.get("lang") or hass.config.language
        # Off the event loop, and one at a time, as anyone may ask for any
        # chart.
        async with RENDER_LOCK:
            svg = await hass.async_add_executor_job(
                render_svg, points, dt_util.now(), language, hass_sun(hass)
            )
        return web.Response(
            body=svg,
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )
