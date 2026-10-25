"""Tests for the color temperature offset math."""

import pytest

from custom_components.light_buddy.offset import (
    ColorTempOffset,
    clamp,
    same_color_temp,
)


def test_kelvin_offset() -> None:
    """A Kelvin offset is added when sending and removed when reading."""
    offset = ColorTempOffset(-300, "K")
    assert offset.apply(3000) == 2700
    assert offset.remove(2700) == 3000


def test_mired_offset() -> None:
    """A mired offset shifts in mired, so positive values are warmer."""
    offset = ColorTempOffset(20, "mired")
    # 4000 K is 250 mired, plus 20 is 270 mired.
    assert offset.apply(4000) == pytest.approx(1_000_000 / 270)
    assert offset.remove(offset.apply(4000)) == pytest.approx(4000)


def test_clamp() -> None:
    """Color temperatures are limited to what a light supports."""
    assert clamp(2000.4, 2200, 4000) == 2200
    assert clamp(4500, 2200, 4000) == 4000
    assert clamp(3000.6, None, None) == 3001


def test_same_color_temp() -> None:
    """Rounding to whole mireds does not count as a change."""
    assert same_color_temp(2700, 2703)
    assert not same_color_temp(2700, 2800)


def test_offsets_stay_in_range() -> None:
    """Large offsets never produce zero, negative or absurd temperatures."""
    assert ColorTempOffset(100, "mired").remove(6500) == 10000
    assert ColorTempOffset(-100, "mired").apply(10000) == 10000
    assert ColorTempOffset(-2000, "K").apply(2700) == 1000
    assert clamp(-300, None, None) == 1000
