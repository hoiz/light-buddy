"""A chart of the schedule, shown in the options flow.

The flow embeds it as an image whose URL carries the points, so changes
that are not saved yet show, too.
"""

from collections.abc import Sequence
import datetime as dt
from functools import lru_cache
from http import HTTPStatus
from xml.sax.saxutils import escape

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS
from homeassistant.util import dt as dt_util

from .schedule import MIRED, SchedulePoint, format_weekdays, target_at

URL = "/api/light_buddy/curve.svg"
MAX_POINTS = 50
MIN_KELVIN = 1000
MAX_KELVIN = 10000

# A Monday, in a zone without daylight saving time, so each day has 24 hours.
REFERENCE_MONDAY = dt.date(2024, 1, 1)
SAMPLE_MINUTES = 10

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


def encode_points(points: Sequence[SchedulePoint]) -> str:
    """Return points as a short text for a URL, e.g. "0530-2200,0630-3000-56"."""
    parts = []
    for point in points:
        if point.time is None:
            continue
        part = f"{point.time:%H%M}-{point.kelvin}"
        if point.weekdays:
            part += "-" + "".join(str(day) for day in sorted(point.weekdays))
        parts.append(part)
    return ",".join(parts)


def decode_points(text: str) -> tuple[SchedulePoint, ...]:
    """Return the points of encode_points, raising ValueError if invalid."""
    parts = text.split(",") if text else []
    if not parts or len(parts) > MAX_POINTS:
        raise ValueError("between 1 and 50 points")
    points = []
    for part in parts:
        fields = part.split("-")
        if (
            len(fields) not in (2, 3)
            or len(fields[0]) != 4
            or not all(field.isdigit() for field in fields)
        ):
            raise ValueError(f"invalid point {part!r}")
        time = dt.time(int(fields[0][:2]), int(fields[0][2:]))
        kelvin = int(fields[1])
        if not MIN_KELVIN <= kelvin <= MAX_KELVIN:
            raise ValueError(f"invalid color temperature {kelvin}")
        weekdays = None
        if len(fields) == 3:
            weekdays = frozenset(int(day) for day in fields[2])
            if not weekdays <= frozenset(range(7)):
                raise ValueError(f"invalid weekdays {fields[2]!r}")
        points.append(SchedulePoint(kelvin, time, weekdays=weekdays))
    return tuple(points)


@lru_cache(maxsize=32)
def day_curves(
    points: tuple[SchedulePoint, ...],
) -> list[tuple[frozenset[int], list[tuple[int, int]]]]:
    """Return the curve of each group of weekdays that share one.

    A curve holds the target in minutes of the day, every few minutes from
    00:00 to 24:00 and at each point, so the line passes through them.
    Cached, as the flow asks for the same chart again and again.
    """
    minutes = sorted(
        set(range(0, 24 * 60 + 1, SAMPLE_MINUTES))
        | {point.time.hour * 60 + point.time.minute for point in points if point.time}
    )
    curves: dict[tuple[tuple[int, int], ...], set[int]] = {}
    for weekday in range(7):
        day = REFERENCE_MONDAY + dt.timedelta(days=weekday)
        start = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
        samples = []
        for minute in minutes:
            target = target_at(points, start + dt.timedelta(minutes=minute))
            if target is None:
                return []
            samples.append((minute, target.kelvin))
        curves.setdefault(tuple(samples), set()).add(weekday)
    return [(frozenset(days), list(samples)) for samples, days in curves.items()]


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


def render_svg(points: Sequence[SchedulePoint], now: dt.datetime, language: str) -> str:
    """Return the schedule as an SVG chart of one day."""
    curves = day_curves(tuple(points))
    all_kelvin = [kelvin for _, samples in curves for _, kelvin in samples]
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
    # interpolates it: the line between two points is straight.
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
    for index, (days, samples) in enumerate(curves):
        dash = DASHES[index % len(DASHES)]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        coordinates = " ".join(
            f"{x(minute):.1f},{y(kelvin):.1f}" for minute, kelvin in samples
        )
        parts.append(
            f'<polyline points="{coordinates}" fill="none" stroke="url(#kelvin)" '
            f'stroke-width="3" stroke-linejoin="round"{dash_attr}/>'
        )
        for point in points:
            if point.time is None or (
                point.weekdays and days.isdisjoint(point.weekdays)
            ):
                continue
            minute = point.time.hour * 60 + point.time.minute
            parts.append(
                f'<circle cx="{x(minute):.1f}" cy="{y(point.kelvin):.1f}" r="4" '
                f'fill="{_color(point.kelvin)}" stroke="{GREY}"/>'
            )
        if len(curves) > 1:
            label_y = TOP + 14 + index * 16
            parts.append(
                f'<line x1="{WIDTH - RIGHT - 150}" y1="{label_y - 4}" '
                f'x2="{WIDTH - RIGHT - 122}" y2="{label_y - 4}" stroke="{GREY}" '
                f'stroke-width="3"{dash_attr}/>'
                f'<text x="{WIDTH - RIGHT - 116}" y="{label_y}">'
                f"{escape(format_weekdays(days, language))}</text>"
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
        # Off the event loop, as anyone may ask for any chart.
        svg = await hass.async_add_executor_job(
            render_svg, points, dt_util.now(), language
        )
        return web.Response(
            body=svg,
            content_type="image/svg+xml",
            headers={"Cache-Control": "no-store"},
        )
