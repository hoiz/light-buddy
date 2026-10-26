"""Tests for the color temperature offset sliders."""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.light_buddy.const import CONF_OFFSETS
from homeassistant.components.light import ColorMode
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import COLOR_LIGHT, PLUG

ON_CT = {"color_mode": ColorMode.COLOR_TEMP, "brightness": 100}


def _slider(hass: HomeAssistant, light: str) -> str:
    """Return the entity id of a light's slider.

    Home Assistant puts the area into new entity ids.
    """
    registry = er.async_get(hass)
    return registry.async_get_entity_id(
        "number", "light_buddy", f"{registry.async_get(light).id}_color_temp_offset"
    )


async def _set_offset(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def test_sliders_for_color_temp_lights(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Color capable room lights get a slider on their device."""
    lamp = add_light("lamp", device_area="Wohnzimmer")
    spot = add_light("spot", area="Wohnzimmer", original_name="Spot")
    add_light("plug", area="Wohnzimmer", attributes=PLUG)
    add_light("outside")
    await setup_integration()

    registry = er.async_get(hass)
    slider = registry.async_get(_slider(hass, lamp))
    assert slider.device_id == registry.async_get(lamp).device_id
    assert slider.entity_category is EntityCategory.CONFIG
    state = hass.states.get(slider.entity_id)
    assert state.name == "lamp Color temperature offset"
    assert state.state == "0"
    assert state.attributes["unit_of_measurement"] == "K"
    assert hass.states.get(_slider(hass, spot)).name == "Spot color temperature offset"
    numbers = [
        entry for entry in registry.entities.values() if entry.domain == "number"
    ]
    assert len(numbers) == 2


async def test_slider_applies_live(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Moving the slider stores the offset and updates a light that is on."""
    lamp = add_light("lamp", device_area="Wohnzimmer")
    entry = await setup_integration()
    hass.states.async_set(
        lamp, STATE_ON, {**COLOR_LIGHT, **ON_CT, "color_temp_kelvin": 3000}
    )
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "light", "turn_on")

    await _set_offset(hass, _slider(hass, lamp), -300)
    registry_id = er.async_get(hass).async_get(lamp).id
    assert entry.options[CONF_OFFSETS] == {registry_id: {"value": -300, "unit": "K"}}
    assert hass.states.get(_slider(hass, lamp)).state == "-300"
    assert [call.data for call in calls] == [
        {"entity_id": [lamp], "color_temp_kelvin": 2700}
    ]

    # The room still shows what was requested, and the next change replaces
    # the offset instead of adding to it.
    hass.states.async_set(
        lamp, STATE_ON, {**COLOR_LIGHT, **ON_CT, "color_temp_kelvin": 2700}
    )
    await hass.async_block_till_done()
    assert (
        hass.states.get("light.all_wohnzimmer").attributes["color_temp_kelvin"] == 3000
    )
    await _set_offset(hass, _slider(hass, lamp), -500)
    # Sent once the room's command interval has passed.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": [lamp], "color_temp_kelvin": 2500}

    await _set_offset(hass, _slider(hass, lamp), 0)
    assert entry.options[CONF_OFFSETS] == {}


async def test_slider_leaves_lights_alone_that_are_off(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Lights that are off or show a color are not changed."""
    lamp = add_light("lamp", device_area="Wohnzimmer")
    await setup_integration()
    calls = async_mock_service(hass, "light", "turn_on")

    await _set_offset(hass, _slider(hass, lamp), -300)
    hass.states.async_set(
        lamp,
        STATE_ON,
        {**COLOR_LIGHT, "color_mode": ColorMode.XY, "hs_color": (0, 100)},
    )
    await hass.async_block_till_done()
    await _set_offset(hass, _slider(hass, lamp), -200)
    assert calls == []


async def test_slider_follows_membership(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Sliders go away with the light's room, offsets are kept."""
    lamp = add_light("lamp", area="Wohnzimmer", original_name="Lamp")
    entry = await setup_integration()
    await _set_offset(hass, _slider(hass, lamp), -300)

    slider = _slider(hass, lamp)
    er.async_get(hass).async_update_entity(lamp, area_id=None)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert hass.states.get(slider) is None
    assert entry.options[CONF_OFFSETS] != {}
    assert hass.states.get(lamp).state == STATE_OFF


def _unit_select(hass: HomeAssistant, light: str) -> str:
    registry = er.async_get(hass)
    return registry.async_get_entity_id(
        "select",
        "light_buddy",
        f"{registry.async_get(light).id}_color_temp_offset_unit",
    )


async def test_unit_switch_converts_offset(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Switching the unit converts the offset and changes the slider."""
    lamp = add_light("lamp", device_area="Wohnzimmer")
    entry = await setup_integration()
    await _set_offset(hass, _slider(hass, lamp), -300)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": _unit_select(hass, lamp), "option": "mired"},
        blocking=True,
    )
    registry_id = er.async_get(hass).async_get(lamp).id
    assert entry.options[CONF_OFFSETS] == {registry_id: {"value": 37, "unit": "mired"}}
    assert hass.states.get(_unit_select(hass, lamp)).state == "mired"
    slider = hass.states.get(_slider(hass, lamp))
    assert slider.state == "37"
    assert slider.attributes["unit_of_measurement"] == "mired"
    assert slider.attributes["max"] == 100
    assert slider.attributes["step"] == 1


async def test_slider_is_not_merged_with_room_commands(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """A slider change during a pending room command goes only to its light."""
    lamp = add_light("lamp", device_area="Wohnzimmer")
    other = add_light("other", device_area="Wohnzimmer")
    await setup_integration()
    hass.states.async_set(
        lamp, STATE_ON, {**COLOR_LIGHT, **ON_CT, "color_temp_kelvin": 3000}
    )
    await hass.async_block_till_done()
    calls = async_mock_service(hass, "light", "turn_on")

    room = hass.data["light"].get_entity("light.all_wohnzimmer")
    await room.async_turn_on(brightness=10)
    await room.async_turn_on(brightness=20)
    await _set_offset(hass, _slider(hass, lamp), -300)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()

    assert [call.data for call in calls] == [
        {"entity_id": [lamp, other], "brightness": 10},
        {"entity_id": [lamp], "color_temp_kelvin": 2700},
        {"entity_id": [lamp, other], "brightness": 20},
    ]


async def test_slider_with_disabled_room_light(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """The offset is stored even when the room light is disabled."""
    lamp = add_light("lamp", device_area="Wohnzimmer")
    entry = await setup_integration()
    er.async_get(hass).async_update_entity(
        "light.all_wohnzimmer", disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    await _set_offset(hass, _slider(hass, lamp), -300)
    assert entry.options[CONF_OFFSETS] != {}


async def test_slider_needs_color_temp(
    hass: HomeAssistant, add_light, setup_integration
) -> None:
    """Color-only lights get no slider, until they support color temperature."""
    lamp = add_light(
        "lamp",
        device_area="Wohnzimmer",
        attributes={"supported_color_modes": [ColorMode.XY]},
        capabilities={"supported_color_modes": [ColorMode.XY]},
    )
    await setup_integration()
    assert _slider(hass, lamp) is None

    er.async_get(hass).async_update_entity(
        lamp, capabilities={"supported_color_modes": [ColorMode.COLOR_TEMP]}
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert _slider(hass, lamp) is not None
