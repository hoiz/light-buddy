"""Tests for resolving the lights of an area."""

from custom_components.light_buddy.membership import async_resolve_rooms
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


async def test_entity_and_device_area(hass: HomeAssistant, add_light) -> None:
    """Lights count for their own area, or their device's area."""
    own = add_light("own", area="Wohnzimmer")
    via_device = add_light("via_device", device_area="Wohnzimmer")
    overridden = add_light(
        "overridden", device_area="Wohnzimmer", area="Küche", original_name="Spot"
    )
    add_light("nowhere")

    assert async_resolve_rooms(hass) == {
        "wohnzimmer": sorted([own, via_device]),
        "kuche": [overridden],
    }


async def test_exclusions(hass: HomeAssistant, add_light) -> None:
    """Groups, own entities, disabled and excluded lights are skipped."""
    lamp = add_light("lamp", area="Wohnzimmer")
    add_light("group", area="Wohnzimmer", platform="group")
    add_light("light_buddy", area="Wohnzimmer", platform="light_buddy")
    add_light(
        "disabled",
        area="Wohnzimmer",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    excluded = add_light("excluded", area="Wohnzimmer")
    add_light("other_room", area="Bad")

    assert async_resolve_rooms(
        hass, excluded_areas=["bad"], excluded_lights=[excluded]
    ) == {"wohnzimmer": [lamp]}
