"""Tests for the Light Buddy config and options flow."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_buddy.const import (
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_OFFSETS,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er


async def _open_step(
    hass: HomeAssistant, entry: MockConfigEntry, step_id: str
) -> ConfigFlowResult:
    """Open the options flow and choose a menu entry."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_user_flow(hass: HomeAssistant) -> None:
    """The user step creates the single entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Light Buddy"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Exclusions are stored, and clearing a selector removes them."""
    lamp = add_light("lamp", area="Wohnzimmer")
    add_light("bath", area="Bad")
    entry = await setup_integration()
    assert hass.states.get("light.all_bad") is not None

    result = await _open_step(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_EXCLUDED_AREAS: ["bad"], CONF_EXCLUDED_LIGHTS: [lamp]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options == {
        CONF_EXCLUDED_AREAS: ["bad"],
        CONF_EXCLUDED_LIGHTS: [lamp],
    }
    assert hass.states.get("light.all_bad") is None
    assert hass.states.get("light.all_wohnzimmer") is None

    result = await _open_step(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options == {CONF_EXCLUDED_AREAS: [], CONF_EXCLUDED_LIGHTS: []}
    assert hass.states.get("light.all_bad") is not None


async def test_offset_flow(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Offsets are stored per light, limited, and removed with 0."""
    lamp = add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration({CONF_EXCLUDED_AREAS: ["bad"]})

    result = await _open_step(hass, entry, "offsets")
    assert result["step_id"] == "offsets"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"light": "light.all_wohnzimmer"}
    )
    assert result["errors"] == {"light": "invalid_light"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"light": lamp}
    )
    assert result["step_id"] == "offset"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"unit": "mired", "value": 150}
    )
    assert result["errors"] == {"value": "offset_too_large"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"unit": "K", "value": -300}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    registry_id = er.async_get(hass).async_get(lamp).id
    assert entry.options == {
        CONF_EXCLUDED_AREAS: ["bad"],
        CONF_OFFSETS: {registry_id: {"value": -300, "unit": "K"}},
    }

    result = await _open_step(hass, entry, "offsets")
    assert "light.lamp: -300 K" in result["description_placeholders"]["offsets"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"light": lamp}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"unit": "K", "value": 0}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_OFFSETS] == {}
