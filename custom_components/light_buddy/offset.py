"""Color temperature offsets for single lights."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_OFFSET_UNIT, CONF_OFFSET_VALUE, UNIT_KELVIN, UNIT_MIRED

# Beyond what any light supports. Keeps results away from zero mired.
MIN_KELVIN = 1000
MAX_KELVIN = 10000

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
            return _mired_to_kelvin(_kelvin_to_mired(kelvin) + self.value)
        return _bound(kelvin + self.value)

    def remove(self, kelvin: float) -> float:
        """Return the requested color temperature for one a light reports."""
        if self.unit == UNIT_MIRED:
            return _mired_to_kelvin(_kelvin_to_mired(kelvin) - self.value)
        return _bound(kelvin - self.value)


@callback
def async_resolve_offsets(
    hass: HomeAssistant, options: Mapping[str, Any]
) -> dict[str, ColorTempOffset]:
    """Return the offsets per entity id.

    Options store offsets by entity registry id, so they survive a changed
    entity id. Offsets of removed entities are skipped.
    """
    registry = er.async_get(hass)
    return {
        entity_id: ColorTempOffset.from_dict(data)
        for registry_id, data in options.items()
        if (entity_id := er.async_resolve_entity_id(registry, registry_id))
    }


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
