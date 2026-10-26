"""Tests for the chart of the schedule."""

import datetime as dt
from http import HTTPStatus
import xml.etree.ElementTree as ET

import pytest

from custom_components.light_buddy.chart import (
    URL,
    day_curves,
    decode_points,
    encode_points,
    render_svg,
)
from custom_components.light_buddy.schedule import DASH, DEFAULT_SCHEDULE, SchedulePoint
from homeassistant.core import HomeAssistant

NOW = dt.datetime(2026, 10, 26, 15, 10)
WEEKEND = (
    SchedulePoint(2200, dt.time(0, 0)),
    SchedulePoint(5000, dt.time(12, 0), weekdays=frozenset(range(5))),
    SchedulePoint(4000, dt.time(12, 0), weekdays=frozenset({5, 6})),
)


def _parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def test_encode_round_trip() -> None:
    """Points survive the URL, weekdays included."""
    text = encode_points(WEEKEND)
    assert text == "0000-2200,1200-5000-01234,1200-4000-56"
    assert decode_points(text) == WEEKEND


@pytest.mark.parametrize(
    "text",
    ["", "junk", "2500-3000", "0630-500", "0630-3000-7", "0630-3000-1-2", "630-3000"],
)
def test_decode_rejects(text: str) -> None:
    """Anything but valid points is rejected."""
    with pytest.raises(ValueError):
        decode_points(text)


def test_decode_rejects_too_many() -> None:
    """At most 50 points."""
    with pytest.raises(ValueError):
        decode_points(",".join(["0630-3000"] * 51))


def test_default_schedule_is_one_curve() -> None:
    """Every day the same: one line, no labels."""
    curves = day_curves(DEFAULT_SCHEDULE)
    assert len(curves) == 1
    assert curves[0][0] == frozenset(range(7))
    root = _parse(render_svg(DEFAULT_SCHEDULE, NOW, "en"))
    namespace = "{http://www.w3.org/2000/svg}"
    assert len(root.findall(f"{namespace}polyline")) == 1
    texts = [text.text for text in root.iter(f"{namespace}text")]
    assert "2000 K" in texts
    assert "5000 K" in texts
    assert f"Mon{DASH}Fri" not in texts


@pytest.mark.parametrize(
    ("language", "labels"),
    [("en", [f"Mon{DASH}Fri", "Sat, Sun"]), ("de", [f"Mo{DASH}Fr", "Sa, So"])],
)
def test_weekend_gets_its_own_curve(language: str, labels: list[str]) -> None:
    """Days with other points get their own, labeled line."""
    root = _parse(render_svg(WEEKEND, NOW, language))
    namespace = "{http://www.w3.org/2000/svg}"
    assert len(root.findall(f"{namespace}polyline")) == 2
    texts = [text.text for text in root.iter(f"{namespace}text")]
    assert all(label in texts for label in labels)


async def test_view(
    hass: HomeAssistant, hass_client_no_auth, setup_integration
) -> None:
    """The chart is served without authentication, invalid points are refused."""
    await setup_integration()
    client = await hass_client_no_auth()

    response = await client.get(URL, params={"points": encode_points(WEEKEND)})
    assert response.status == HTTPStatus.OK
    assert response.content_type == "image/svg+xml"
    assert response.headers["Cache-Control"] == "no-store"
    _parse(await response.text())

    response = await client.get(URL, params={"points": "junk"})
    assert response.status == HTTPStatus.BAD_REQUEST


def _circles_and_labels(svg: str) -> tuple[list[float], list[str]]:
    root = _parse(svg)
    namespace = "{http://www.w3.org/2000/svg}"
    return (
        [float(circle.get("cy")) for circle in root.iter(f"{namespace}circle")],
        [text.text for text in root.iter(f"{namespace}text")],
    )


def test_points_between_samples() -> None:
    """The line and the axis reach points off the sampling grid."""
    points = decode_points("1200-2000,1205-9000,1210-2000")
    curve = day_curves(points)[0][1]
    assert (725, 9000) in curve
    heights, labels = _circles_and_labels(render_svg(points, NOW, "en"))
    assert all(0 <= height <= 260 for height in heights)
    # The axis reaches the next round value.
    assert "10000 K" in labels


def test_top_tick() -> None:
    """The axis ends on labeled ticks, also between round values."""
    points = decode_points("0000-2100,1200-6000")
    _, labels = _circles_and_labels(render_svg(points, NOW, "en"))
    assert "2000 K" in labels
    assert "6500 K" in labels


def test_points_on_some_days_only() -> None:
    """A schedule for Mondays only still has a curve for every day."""
    points = decode_points("0600-3000-0,1800-5000-0")
    curves = day_curves(points)
    assert sorted(day for days, _ in curves for day in days) == list(range(7))
    _parse(render_svg(points, NOW, "de"))
