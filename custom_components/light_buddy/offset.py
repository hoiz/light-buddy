"""Color temperature offsets for single lights, in Kelvin."""

from collections.abc import Mapping

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

# Beyond what any light supports. Keeps results away from zero mired.
MIN_KELVIN = 1000
MAX_KELVIN = 10000

# Values a light reports back differ from what was sent, because Zigbee lights
# work in whole mireds. Two mireds are below what the eye can tell apart.
MIRED_TOLERANCE = 2


@callback
def async_resolve_offsets(
    hass: HomeAssistant, options: Mapping[str, int]
) -> dict[str, int]:
    """Return the offsets per entity id.

    Options store offsets by entity registry id, so they survive a changed
    entity id. Offsets of removed entities are skipped.
    """
    registry = er.async_get(hass)
    return {
        entity_id: offset
        for registry_id, offset in options.items()
        if (entity_id := er.async_resolve_entity_id(registry, registry_id))
    }


def apply_offset(kelvin: float, offset: int) -> float:
    """Return the color temperature to send for a requested one.

    A negative offset makes a light warmer.
    """
    return _bound(kelvin + offset)


def remove_offset(kelvin: float, offset: int) -> float:
    """Return the requested color temperature for one a light reports."""
    return _bound(kelvin - offset)


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
    mired_a = 1_000_000 / _bound(kelvin_a)
    mired_b = 1_000_000 / _bound(kelvin_b)
    return abs(mired_a - mired_b) <= MIRED_TOLERANCE


def _bound(kelvin: float) -> float:
    return min(max(kelvin, MIN_KELVIN), MAX_KELVIN)
