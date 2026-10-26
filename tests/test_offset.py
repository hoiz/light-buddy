"""Tests for the color temperature offset math."""

from custom_components.light_buddy.offset import (
    apply_offset,
    clamp,
    remove_offset,
    same_color_temp,
)


def test_offset() -> None:
    """An offset is added when sending and removed when reading."""
    assert apply_offset(3000, -300) == 2700
    assert remove_offset(2700, -300) == 3000


def test_offsets_stay_in_range() -> None:
    """Offsets never produce zero, negative or absurd temperatures."""
    assert apply_offset(2700, -2000) == 1000
    assert remove_offset(9500, -1500) == 10000
    assert clamp(-300, None, None) == 1000


def test_clamp() -> None:
    """Color temperatures are limited to what a light supports."""
    assert clamp(2000.4, 2200, 4000) == 2200
    assert clamp(4500, 2200, 4000) == 4000
    assert clamp(3000.6, None, None) == 3001


def test_same_color_temp() -> None:
    """Rounding to whole mireds does not count as a change."""
    assert same_color_temp(2700, 2703)
    assert not same_color_temp(2700, 2800)
