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
    assert entry.options == {
        CONF_EXCLUDED_AREAS: ["bad"],
        CONF_EXCLUDED_LIGHTS: [lamp],
    }
    assert hass.states.get("light.all_bad") is None
    assert hass.states.get("light.all_wohnzimmer") is None

    result = await _open(hass, entry, "rooms")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert entry.options == {CONF_EXCLUDED_AREAS: [], CONF_EXCLUDED_LIGHTS: []}
    assert hass.states.get("light.all_bad") is not None


async def _schedule(hass: HomeAssistant, entry, flow_id: str, step_id: str, data=None):
    """Pick a schedule menu entry, and submit its form if data is given."""
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": step_id}
    )
    if data is not None:
        result = await hass.config_entries.options.async_configure(flow_id, data)
    return result


def _suggested(result) -> dict:
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def test_schedule_add_edit_delete(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Points are added sorted, edited with their values, and deleted."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration()
    result = await _open(hass, entry, "schedule")
    assert result["type"] is FlowResultType.MENU
    assert "| 12:30 | 5000 K |" in result["description_placeholders"]["points"]
    flow_id = result["flow_id"]

    result = await _schedule(
        hass,
        entry,
        flow_id,
        "add_point",
        {"time": "14:00:00", "kelvin": 4800, "brightness": 80, "weekdays": ["5", "6"]},
    )
    assert result["step_id"] == "schedule"
    assert (
        "| 14:00 | 4800 K | 80 % | Sat, Sun |"
        in (result["description_placeholders"]["points"])
    )

    # The default has 8 points, sorted by time: 12:30 is the 4th, 14:00 the 5th.
    result = await _schedule(hass, entry, flow_id, "edit_point", {"point": "4"})
    assert result["step_id"] == "point"
    assert _suggested(result) == {
        "time": "14:00:00",
        "kelvin": 4800,
        "brightness": 80,
        "weekdays": ["5", "6"],
    }
    result = await hass.config_entries.options.async_configure(
        flow_id, {"time": "14:00:00", "kelvin": 4700}
    )
    assert "| 14:00 | 4700 K |" in result["description_placeholders"]["points"]

    result = await _schedule(hass, entry, flow_id, "delete_points", {"points": ["0"]})
    assert "05:30" not in result["description_placeholders"]["points"]

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "save_schedule"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    schedule = entry.options["schedule"]
    assert [point["time"] for point in schedule] == [
        "06:30",
        "08:00",
        "12:30",
        "14:00",
        "16:00",
        "18:30",
        "21:00",
        "22:30",
    ]
    assert schedule[3] == {
        "time": "14:00",
        "kelvin": 4700,
        "brightness": None,
        "weekdays": [],
    }


async def test_schedule_validation(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Points cannot share a time on a day, and one has to stay."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration()
    flow_id = (await _open(hass, entry, "schedule"))["flow_id"]

    result = await _schedule(
        hass, entry, flow_id, "add_point", {"time": "12:30:00", "kelvin": 4000}
    )
    assert result["errors"] == {"base": "duplicate_point"}
    # On weekends only, it would still collide with the point for every day.
    result = await hass.config_entries.options.async_configure(
        flow_id, {"time": "12:30:00", "kelvin": 4000, "weekdays": ["6"]}
    )
    assert result["errors"] == {"base": "duplicate_point"}
    result = await hass.config_entries.options.async_configure(
        flow_id, {"time": "12:45:00", "kelvin": 4000}
    )
    assert result["step_id"] == "schedule"

    result = await _schedule(
        hass,
        entry,
        flow_id,
        "delete_points",
        {"points": [str(index) for index in range(9)]},
    )
    assert result["errors"] == {"base": "keep_one_point"}


async def test_weekday_points_at_same_time(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """The same time on different days is allowed."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration(
        {
            "schedule": [
                {
                    "time": "07:00",
                    "kelvin": 5000,
                    "brightness": None,
                    "weekdays": [0, 1, 2, 3, 4],
                },
            ]
        }
    )
    flow_id = (await _open(hass, entry, "schedule"))["flow_id"]
    result = await _schedule(
        hass,
        entry,
        flow_id,
        "add_point",
        {"time": "07:00:00", "kelvin": 3000, "weekdays": ["5", "6"]},
    )
    assert result["step_id"] == "schedule"


async def test_schedule_reset_and_cancel(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Reset needs confirming, and nothing is stored before saving."""
    add_light("lamp", area="Wohnzimmer")
    stored = [{"time": "07:00", "kelvin": 5000, "brightness": None, "weekdays": []}]
    entry = await setup_integration({"schedule": stored})
    flow_id = (await _open(hass, entry, "schedule"))["flow_id"]

    result = await _schedule(hass, entry, flow_id, "reset_schedule", {"confirm": False})
    assert "| 07:00 | 5000 K |" in result["description_placeholders"]["points"]
    result = await _schedule(hass, entry, flow_id, "reset_schedule", {"confirm": True})
    assert "| 12:30 | 5000 K |" in result["description_placeholders"]["points"]

    # Leaving without saving keeps the stored schedule.
    hass.config_entries.options.async_abort(flow_id)
    assert entry.options["schedule"] == stored


async def test_settings(hass: HomeAssistant, add_light, setup_integration) -> None:
    """Settings show their defaults and are stored."""
    add_light("lamp", area="Wohnzimmer")
    entry = await setup_integration()
    result = await _open(hass, entry, "settings")
    assert _suggested(result) == {
        "threshold": 50,
        "transition": 2,
        "auto_resume_minutes": 0,
        "separate_commands": "off",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "threshold": 100,
            "transition": 1.5,
            "auto_resume_minutes": 30,
            "separate_commands": "ikea",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["settings"] == {
        "threshold": 100,
        "transition": 1.5,
        "auto_resume_minutes": 30,
        "separate_commands": "ikea",
    }
