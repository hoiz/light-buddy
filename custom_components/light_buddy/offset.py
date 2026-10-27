"""Color temperature offsets for single lights, in Kelvin or mired."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from .const import (
    CONF_OFFSET_UNIT,
    CONF_OFFSET_VALUE,
    OLD_UNIT_KELVIN,
    UNIT_KELVIN,
    UNIT_MIRED,
)

# Beyond what any light supports. Keeps results away from zero mired.
MIN_KELVIN = 1000
MAX_KELVIN = 10000

# Values a light reports back differ from what was sent, because Zigbee lights
# work in whole mireds. Two mireds are below what the eye can tell apart.
MIRED_TOLERANCE = 2

# Switching the unit keeps the offset's effect at this color temperature.
REFERENCE_KELVIN = 3000

# Largest offset and slider step per unit.
OFFSET_LIMITS = {UNIT_KELVIN: 1500, UNIT_MIRED: 100}
OFFSET_STEPS = {UNIT_KELVIN: 25, UNIT_MIRED: 1}
# Shown next to the slider.
UNIT_SYMBOLS = {UNIT_KELVIN: "K", UNIT_MIRED: "mired"}


@dataclass(frozen=True, slots=True)
class ColorTempOffset:
    """Shift the color temperature of a light.

    In Kelvin, a negative offset makes a light warmer. In mired, a positive
    offset makes it warmer, and looks equally strong at every color
    temperature.
    """

    value: float = 0
    unit: str = UNIT_KELVIN

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Create an offset from its stored options."""
        unit = data[CONF_OFFSET_UNIT]
        return cls(
            data[CONF_OFFSET_VALUE], UNIT_KELVIN if unit == OLD_UNIT_KELVIN else unit
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the offset for storing in the options."""
        return {CONF_OFFSET_VALUE: self.value, CONF_OFFSET_UNIT: self.unit}

    def apply(self, kelvin: float) -> float:
        """Return the color temperature to send for a requested one."""
        if self.unit == UNIT_MIRED:
            return _mired_to_kelvin(_kelvin_to_mired(kelvin) + self.value)
        return _bound(kelvin + self.value)

    def remove(self, kelvin: float) -> float:
        """Return the requested color temperature for one a light reports."""
        if self.unit == UNIT_MIRED:
            return _mired_to_kelvin(_kelvin_to_mired(kelvin) - self.value)
        return _bound(kelvin - self.value)

    def with_value(self, value: float) -> ColorTempOffset:
        """Return the offset with another value, limited to its unit."""
        limit = OFFSET_LIMITS[self.unit]
        step = OFFSET_STEPS[self.unit]
        value = round(min(max(value, -limit), limit) / step) * step
        return ColorTempOffset(value, self.unit)

    def converted(self, unit: str) -> ColorTempOffset:
        """Return an offset in another unit with the same effect at 3000 K."""
        if unit == self.unit:
            return self
        shifted = self.apply(REFERENCE_KELVIN)
        if unit == UNIT_MIRED:
            value = _kelvin_to_mired(shifted) - _kelvin_to_mired(REFERENCE_KELVIN)
        else:
            value = shifted - REFERENCE_KELVIN
        return ColorTempOffset(unit=unit).with_value(value)


def clamp(kelvin: float, min_kelvin: int | None, max_kelvin: int | None) -> int:
    """Round and limit a color temperature to what a light supports."""
    kelvin = _bound(kelvin)
    if min_kelvin is not None:
        kelvin = max(kelvin, min_kelvin)
    if max_kelvin is not None:
        kelvin = min(kelvin, max_kelvin)
    return round(kelvin)


def same_color_temp(kelvin_a: float, kelvin_b: float) -> bool:
    """Return True if both color temperatures look the same."""
    return abs(_kelvin_to_mired(kelvin_a) - _kelvin_to_mired(kelvin_b)) <= (
        MIRED_TOLERANCE
    )


def _bound(kelvin: float) -> float:
    return min(max(kelvin, MIN_KELVIN), MAX_KELVIN)


# The color helpers of Home Assistant round down to whole mireds, which would
# make the shown color temperature drift from the requested one.
def _kelvin_to_mired(kelvin: float) -> float:
    return 1_000_000 / _bound(kelvin)


def _mired_to_kelvin(mired: float) -> float:
    return _bound(1_000_000 / max(mired, 1_000_000 / MAX_KELVIN))
