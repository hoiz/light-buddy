"""Tests for setting up and migrating Light Buddy."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_buddy.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


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
