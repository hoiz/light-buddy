"""Resolve which lights belong to which area."""

from collections import defaultdict
from collections.abc import Iterable

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

# Light groups would make their members receive every command twice. Groups of
# other platforms (e.g. template lights) can be excluded in the options.
GROUP_PLATFORMS = frozenset({"group"})


@callback
def async_resolve_rooms(
    hass: HomeAssistant,
    excluded_areas: Iterable[str] = (),
    excluded_lights: Iterable[str] = (),
) -> dict[str, list[str]]:
    """Return a mapping of area id to the sorted light entity ids in that area.

    A light belongs to its own area, or to its device's area if it has none.
    Areas without lights are not part of the result.
    """
    excluded_areas = set(excluded_areas)
    excluded_lights = set(excluded_lights)
    rooms: defaultdict[str, list[str]] = defaultdict(list)

    for entry in er.async_get(hass).entities.values():
        if (
            entry.domain != LIGHT_DOMAIN
            or entry.platform == DOMAIN
            or entry.disabled_by is not None
            or entry.entity_id in excluded_lights
            or entry.platform in GROUP_PLATFORMS
        ):
            continue
        area_id = er.async_get_effective_area_id(hass, entry)
        if area_id is None or area_id in excluded_areas:
            continue
        rooms[area_id].append(entry.entity_id)

    return {area_id: sorted(entity_ids) for area_id, entity_ids in rooms.items()}
