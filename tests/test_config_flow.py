"""Tests for the Light Buddy config and options flow."""

from custom_components.light_buddy.const import (
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def _open(hass: HomeAssistant, entry, step_id: str):
    """Open the options flow at a menu entry."""
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

    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_EXCLUDED_AREAS: ["bad"], CONF_EXCLUDED_LIGHTS: [lamp]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_EXCLUDED_AREAS] == ["bad"]
    assert entry.options[CONF_EXCLUDED_LIGHTS] == [lamp]
    assert hass.states.get("light.all_bad") is None
    assert hass.states.get("light.all_wohnzimmer") is None

    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options[CONF_EXCLUDED_AREAS] == []
    assert entry.options[CONF_EXCLUDED_LIGHTS] == []
    assert hass.states.get("light.all_bad") is not None


def _suggested(result) -> dict:
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def test_settings(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Settings show their defaults and are stored."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration(
        {"schedule": [{"time": "07:00", "kelvin": 5000}]}, sun=True
    )
    result = await _open(hass, entry, "settings")
    # The chart of the saved schedule.
    assert result["description_placeholders"]["chart"].startswith(
        "/api/light_buddy/curve.svg?points=r%2B0_w0600-0730_k2700"
    )
    assert _suggested(result) == {
        "warmest": 2700,
        "coldest": 5000,
        "threshold": 50,
        "transition": 2,
        "auto_resume_minutes": 0,
        "separate_commands": "off",
        "disco_interval": 4,
        "disco_palette": "rainbow",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "warmest": 3500,
            "coldest": 3800,
            "threshold": 100,
            "transition": 1.5,
            "auto_resume_minutes": 30,
            "separate_commands": "ikea",
            "disco_interval": 2,
            "disco_palette": "party",
        },
    )
    assert result["errors"] == {"base": "schedule_span"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "warmest": 3000,
            "coldest": 5500,
            "threshold": 100,
            "transition": 1.5,
            "auto_resume_minutes": 30,
            "separate_commands": "ikea",
            "disco_interval": 2,
            "disco_palette": "party",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # A schedule of points from before is dropped.
    assert "schedule" not in entry.options
    assert entry.options["settings"] == {
        "warmest": 3000,
        "coldest": 5500,
        "threshold": 100,
        "transition": 1.5,
        "auto_resume_minutes": 30,
        "separate_commands": "ikea",
        "disco_interval": 2,
        "disco_palette": "party",
    }
