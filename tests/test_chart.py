"""Tests for the chart of the schedule."""

import datetime as dt
from http import HTTPStatus
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import pytest

from custom_components.light_buddy.chart import (
    URL,
    curves,
    decode_points,
    encode_points,
    render_svg,
)
from custom_components.light_buddy.schedule import (
    DASH,
    DEFAULT_SCHEDULE,
    SchedulePoint,
    Sun,
)
from homeassistant.core import HomeAssistant

TZ = ZoneInfo("Europe/Berlin")
BERLIN = Sun(52.52, 13.40)
# A Monday afternoon.
NOW = dt.datetime(2026, 10, 26, 15, 10, tzinfo=TZ)
SVG = "{http://www.w3.org/2000/svg}"
WEEKEND = (
    SchedulePoint(2200, dt.time(0, 0)),
    SchedulePoint(5000, dt.time(12, 0), weekdays=frozenset(range(5))),
    SchedulePoint(4000, dt.time(12, 0), weekdays=frozenset({5, 6})),
)


def _parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _texts(svg: str) -> list[str]:
    return [text.text for text in _parse(svg).iter(f"{SVG}text")]


def test_encode_round_trip() -> None:
    """Points survive the URL: clock and sun points, windows and weekdays."""
    text = encode_points(WEEKEND)
    assert text == "0000_k2200,1200_k5000_d01234,1200_k4000_d56"
    assert decode_points(text) == WEEKEND
    text = encode_points(DEFAULT_SCHEDULE)
    assert (
        text
        == "r+0_w0600-0730_k2700,r+120_w0800-0900_k5000,n+0_k5000,s+0_k3200,s+90_w-2200_k2700"
    )
    assert decode_points(text) == DEFAULT_SCHEDULE


@pytest.mark.parametrize(
    "text",
    [
        "",
        "junk",
        "2500_k3000",
        "0630_k500",
        "0630_k3000_d7",
        "0630_k3000_d1_x",
        "630_k3000",
        "0630_w0600-0700_k3000",
        "x+0_k3000",
        "s+99999_k3000",
        "s+0_w2500-_k3000",
        "s+0_w06-07_k3000",
        "s+0",
    ],
)
def test_decode_rejects(text: str) -> None:
    """Anything but valid points is rejected."""
    with pytest.raises(ValueError):
        decode_points(text)


def test_decode_rejects_too_many() -> None:
    """At most 50 points."""
    with pytest.raises(ValueError):
        decode_points(",".join(["0630_k3000"] * 51))


def test_default_schedule() -> None:
    """Today's curve, and the longest and shortest day next to it."""
    day_curves, season_curves = curves(DEFAULT_SCHEDULE, NOW.date(), TZ, BERLIN)
    assert len(day_curves) == 1
    assert [index for index, _ in season_curves] == [0, 1]
    root = _parse(render_svg(DEFAULT_SCHEDULE, NOW, "en", BERLIN))
    assert len(root.findall(f"{SVG}polyline")) == 3
    texts = _texts(render_svg(DEFAULT_SCHEDULE, NOW, "de", BERLIN))
    assert "Längster Tag" in texts
    assert "Kürzester Tag" in texts
    assert "5000 K" in texts


def test_clock_points_change_with_the_seasons() -> None:
    """Between clock points the curve follows daylight, which the seasons change."""
    points = decode_points("0000_k2200,1200_k5000")
    _, season_curves = curves(points, NOW.date(), TZ, BERLIN)
    assert [index for index, _ in season_curves] == [0, 1]
    # Without the sun, there is nothing to compare.
    assert curves(points, NOW.date(), TZ, None)[1] == []


@pytest.mark.parametrize(
    ("language", "labels"),
    [("en", [f"Mon{DASH}Fri", "Sat, Sun"]), ("de", [f"Mo{DASH}Fr", "Sa, So"])],
)
def test_weekend_gets_its_own_curve(language: str, labels: list[str]) -> None:
    """Days with other points get their own, labeled line."""
    root = _parse(render_svg(WEEKEND, NOW, language, BERLIN))
    # Two kinds of days, and the longest and shortest day.
    assert len(root.findall(f"{SVG}polyline")) == 4
    texts = [text.text for text in root.iter(f"{SVG}text")]
    assert all(label in texts for label in labels)


def _circles_and_labels(svg: str) -> tuple[list[float], list[str]]:
    root = _parse(svg)
    return (
        [float(circle.get("cy")) for circle in root.iter(f"{SVG}circle")],
        [text.text for text in root.iter(f"{SVG}text")],
    )


def test_points_between_samples() -> None:
    """The line and the axis reach points off the sampling grid."""
    points = decode_points("1200_k2000,1205_k9000,1210_k2000")
    day_curves, _ = curves(points, NOW.date(), TZ, BERLIN)
    assert (725, 9000) in day_curves[0][2]
    heights, labels = _circles_and_labels(render_svg(points, NOW, "en", BERLIN))
    assert all(0 <= height <= 260 for height in heights)
    assert "10000 K" in labels


def test_top_tick() -> None:
    """The axis ends on labeled ticks, also between round values."""
    points = decode_points("0000_k2100,1200_k6000")
    _, labels = _circles_and_labels(render_svg(points, NOW, "en", BERLIN))
    assert "2000 K" in labels
    assert "6500 K" in labels


def test_points_on_some_days_only() -> None:
    """A schedule for Mondays only still has a curve for every day."""
    points = decode_points("0600_k3000_d0,1800_k5000_d0")
    day_curves, _ = curves(points, NOW.date(), TZ, BERLIN)
    assert sorted(day for days, _, _ in day_curves for day in days) == list(range(7))
    _parse(render_svg(points, NOW, "de", BERLIN))


async def test_view(
    hass: HomeAssistant, hass_client_no_auth, setup_integration
) -> None:
    """The chart is served without authentication, invalid points are refused."""
    await setup_integration()
    client = await hass_client_no_auth()

    response = await client.get(URL, params={"points": encode_points(DEFAULT_SCHEDULE)})
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/svg+xml"
    assert response.headers["Cache-Control"] == "no-store"
    _parse(await response.text())

    response = await client.get(URL, params={"points": "junk"})
    assert response.status == HTTPStatus.BAD_REQUEST
