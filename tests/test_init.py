"""Tests for setting up and migrating Light Buddy."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_buddy.const import DOMAIN
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT, PLUG, WHITE_LIGHT


async def test_offset_entity_ids_are_english(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Offset settings get entity ids from their light, whatever the language."""
    hass.config.language = "de"
    add_light("stehlampe", device_area="Wohnzimmer")
    await setup_integration()

    assert hass.states.get("number.stehlampe_color_temperature_offset") is not None
    assert hass.states.get("select.stehlampe_color_temperature_offset_unit") is not None


async def test_migrate_german_entity_ids(hass: HomeAssistant, add_light) -> None:
    """Entity ids from the language of Home Assistant become English once."""
    lamp = add_light("stehlampe", device_area="Wohnzimmer")
    add_light("taken", device_area="Wohnzimmer")
    registry = er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1)
    entry.add_to_hass(hass)
    lamp_id = registry.async_get(lamp).id
    taken_id = registry.async_get("light.taken").id
    for domain, unique_id, object_id in (
        ("number", f"{lamp_id}_color_temp_offset", "stehlampe_farbtemperatur_offset"),
        (
            "select",
            f"{lamp_id}_color_temp_offset_unit",
            "stehlampe_einheit_farbtemperatur_offset",
        ),
        ("number", f"{taken_id}_color_temp_offset", "taken_farbtemperatur_offset"),
    ):
        registry.async_get_or_create(
            domain,
            DOMAIN,
            unique_id,
            suggested_object_id=object_id,
            config_entry=entry,
        )
    # Somebody else already uses the new entity id.
    registry.async_get_or_create(
        "number", "other", "x", suggested_object_id="taken_color_temperature_offset"
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert registry.async_get_entity_id(
        "number", DOMAIN, f"{lamp_id}_color_temp_offset"
    ) == ("number.stehlampe_color_temperature_offset")
    assert registry.async_get_entity_id(
        "select", DOMAIN, f"{lamp_id}_color_temp_offset_unit"
    ) == ("select.stehlampe_color_temperature_offset_unit")
    assert registry.async_get_entity_id(
        "number", DOMAIN, f"{taken_id}_color_temp_offset"
    ) == ("number.taken_farbtemperatur_offset")


async def test_exclusions_reload_but_own_options_do_not(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Changed exclusions reload, options the entities write do not."""
    from unittest.mock import patch

    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration()
    with patch.object(hass.config_entries, "async_reload") as reload:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "schedules_off": ["wohnzimmer"]}
        )
        await hass.async_block_till_done()
        assert not reload.called

        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "excluded_areas": ["wohnzimmer"]}
        )
        await hass.async_block_till_done()
        assert reload.called


async def test_one_target_sensor(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """All rooms share one target sensor, older entities of rooms are removed."""
    add_light("a", area="Wohnzimmer")
    add_light("b", area="Küche")
    entry = MockConfigEntry(domain=DOMAIN, title="Light Buddy")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for platform, unique_id in (
        ("sensor", "wohnzimmer_target_color_temp"),
        ("button", "wohnzimmer_resume_schedule"),
    ):
        registry.async_get_or_create(platform, DOMAIN, unique_id, config_entry=entry)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entities = [
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(
            registry, entry.entry_id
        )
        if registry_entry.domain in ("sensor", "button")
    ]
    assert entities == ["sensor.target_color_temperature"]
    state = hass.states.get("sensor.target_color_temperature")
    assert state.name == "Target color temperature"
    assert state.attributes["unit_of_measurement"] == "K"


async def test_room_switches_join_their_area(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Schedule and disco switches are configuration entities in their area."""
    add_light("lamp", area="Wohnzimmer")
    await setup_integration()
    registry = er.async_get(hass)

    for entity_id in ("switch.schedule_wohnzimmer", "switch.disco_wohnzimmer"):
        entry = registry.async_get(entity_id)
        assert entry.area_id == "wohnzimmer"
        assert entry.entity_category is er.EntityCategory.CONFIG
    # The room light stays outside, so area actions do not reach lamps twice.
    assert registry.async_get("light.all_wohnzimmer").area_id is None


async def test_area_changed_by_hand_is_kept(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """An area removed by hand is not set again after a reload."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration()
    registry = er.async_get(hass)
    registry.async_update_entity("switch.schedule_wohnzimmer", area_id=None)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get("switch.schedule_wohnzimmer").area_id is None
    assert registry.async_get("switch.disco_wohnzimmer").area_id == "wohnzimmer"


async def test_area_actions_skip_room_switches(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Turning off everything in an area leaves the schedule on."""
    add_light("lamp", area="Wohnzimmer")
    await setup_integration()
    assert await async_setup_component(hass, "homeassistant", {})

    await hass.services.async_call(
        "homeassistant", "turn_off", {"area_id": "wohnzimmer"}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_off", {"area_id": "wohnzimmer"}, blocking=True
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.schedule_wohnzimmer").state == "on"


@pytest.mark.parametrize(
    ("attributes", "switches"),
    [
        (COLOR_LIGHT, {"schedule", "disco"}),
        (WHITE_LIGHT, {"schedule"}),
        ({"supported_color_modes": ["hs"], "supported_features": 0}, {"disco"}),
        ({"supported_color_modes": ["brightness"], "supported_features": 0}, set()),
        (PLUG, set()),
    ],
)
async def test_switches_fit_the_lights(
    hass: HomeAssistant,
    add_light,
    setup_integration,
    attributes: dict,
    switches: set[str],
) -> None:
    """A room gets the switches its lights can use, and always its light."""
    add_light("lamp", area="Wohnzimmer", attributes=attributes)
    await setup_integration()

    assert hass.states.get("light.all_wohnzimmer") is not None
    present = {
        name
        for name in ("schedule", "disco")
        if hass.states.get(f"switch.{name}_wohnzimmer") is not None
    }
    assert present == switches


async def test_switches_follow_the_lights(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light with colors added later brings the disco switch, and takes it."""
    add_light("white", area="Wohnzimmer", attributes=WHITE_LIGHT)
    await setup_integration()
    assert hass.states.get("switch.disco_wohnzimmer") is None

    color = add_light("color", area="Wohnzimmer", attributes=COLOR_LIGHT)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert hass.states.get("switch.disco_wohnzimmer") is not None

    er.async_get(hass).async_remove(color)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert hass.states.get("switch.disco_wohnzimmer") is None
    assert hass.states.get("switch.schedule_wohnzimmer") is not None


async def _refresh(hass: HomeAssistant) -> None:
    """Let the debounced refresh after registry changes run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()


async def test_disco_ends_with_its_switch(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """When the last light with colors leaves, a running disco ends."""
    color = add_light("color", area="Wohnzimmer", state=STATE_ON)
    white = add_light("white", area="Wohnzimmer", attributes=WHITE_LIGHT)
    entry = await setup_integration()
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.disco_wohnzimmer"}, blocking=True
    )
    automation = entry.runtime_data._automations["wohnzimmer"]
    assert automation.disco.running
    calls = async_mock_service(hass, "light", "turn_on")

    er.async_get(hass).async_update_entity(color, area_id=None)
    ar.async_get(hass).async_get_or_create("Küche")
    er.async_get(hass).async_update_entity(color, area_id="kuche")
    await _refresh(hass)

    assert hass.states.get("switch.disco_wohnzimmer") is None
    assert not automation.disco.running
    # The color light belongs to the Küche now, whose schedule takes it
    # over; the white light was off before disco and stays off.
    assert calls == []
    assert hass.states.get(white).state == STATE_OFF


async def test_other_switch_stays_while_one_comes(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Adding the disco switch leaves the schedule switch as it is."""
    add_light("white", area="Wohnzimmer", attributes=WHITE_LIGHT)
    await setup_integration()
    seen: list[str] = []
    async_track_state_change_event(
        hass,
        "switch.schedule_wohnzimmer",
        lambda event: seen.append(event.data["new_state"].state),
    )

    add_light("color", area="Wohnzimmer")
    await _refresh(hass)

    assert hass.states.get("switch.disco_wohnzimmer") is not None
    assert seen == []


async def test_unknown_light_keeps_switches(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A light whose color modes are not known takes no switch away."""
    color = add_light("color", area="Wohnzimmer")
    await setup_integration()
    hass.states.async_remove(color)

    # Any registry change of the light refreshes the rooms.
    er.async_get(hass).async_update_entity(color, name="Lamp")
    await _refresh(hass)

    assert hass.states.get("switch.disco_wohnzimmer") is not None
    assert hass.states.get("switch.schedule_wohnzimmer") is not None
