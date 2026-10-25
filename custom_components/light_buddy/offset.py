"""Color temperature offsets for single lights."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from .const import CONF_OFFSET_UNIT, CONF_OFFSET_VALUE, UNIT_KELVIN, UNIT_MIRED

# Values a light reports back differ from what was sent, because many lights
# work in whole mireds. Two mireds are below what the eye can tell apart.
MIRED_TOLERANCE = 2


@dataclass(frozen=True, slots=True)
class ColorTempOffset:
    """Shift the color temperature of a light, in Kelvin or mired.

    In Kelvin, a negative offset makes a light warmer. In mired, a positive
    offset makes it warmer.
    """

    value: float
    unit: str = UNIT_KELVIN

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Create an offset from its stored options."""
        return cls(data[CONF_OFFSET_VALUE], data.get(CONF_OFFSET_UNIT, UNIT_KELVIN))

    def apply(self, kelvin: float) -> float:
        """Return the color temperature to send for a requested one."""
        if self.unit == UNIT_MIRED:
            return _mired(_mired(kelvin) + self.value)
        return kelvin + self.value

    def remove(self, kelvin: float) -> float:
        """Return the requested color temperature for one a light reports."""
        if self.unit == UNIT_MIRED:
            return _mired(_mired(kelvin) - self.value)
        return kelvin - self.value


def offsets_from_options(options: Mapping[str, Any]) -> dict[str, ColorTempOffset]:
    """Return the offsets per light entity id."""
    return {
        entity_id: ColorTempOffset.from_dict(data)
        for entity_id, data in options.items()
    }


def clamp(kelvin: float, min_kelvin: int | None, max_kelvin: int | None) -> int:
    """Round and limit a color temperature to what a light supports."""
    if min_kelvin is not None:
        kelvin = max(kelvin, min_kelvin)
    if max_kelvin is not None:
        kelvin = min(kelvin, max_kelvin)
    return round(kelvin)


def same_color_temp(kelvin_a: float, kelvin_b: float) -> bool:
    """Return True if both color temperatures look the same."""
    return abs(_mired(kelvin_a) - _mired(kelvin_b)) <= MIRED_TOLERANCE


def _mired(value: float) -> float:
    """Convert between Kelvin and mired, which is the same formula both ways."""
    return 1_000_000 / max(value, 1)
