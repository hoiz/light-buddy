"""Config and options flow for Light Buddy."""

from typing import Any, override

import probatio

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_LIGHT,
    CONF_OFFSET_UNIT,
    CONF_OFFSET_VALUE,
    CONF_OFFSETS,
    DOMAIN,
    UNIT_KELVIN,
    UNIT_MIRED,
)
from .membership import GROUP_PLATFORMS

# Larger shifts would leave the range of common lights, e.g. 2700 K plus 100
# mired is about 2100 K.
OFFSET_LIMITS = {UNIT_KELVIN: 2000, UNIT_MIRED: 100}
LIMIT_PLACEHOLDERS = {
    "max_kelvin": str(OFFSET_LIMITS[UNIT_KELVIN]),
    "max_mired": str(OFFSET_LIMITS[UNIT_MIRED]),
}


class LightBuddyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Light Buddy. Rooms are discovered from the area registry."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        if user_input is not None:
            return self.async_create_entry(title="Light Buddy", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> LightBuddyOptionsFlow:
        """Return the options flow."""
        return LightBuddyOptionsFlow()


class LightBuddyOptionsFlow(OptionsFlowWithReload):
    """Exclude areas or lights, and set color temperature offsets."""

    _light: str
    _light_entity_id: str

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(step_id="init", menu_options=["rooms", "offsets"])

    async def async_step_rooms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the excluded areas and lights."""
        if user_input is not None:
            # Cleared selectors are missing from user_input.
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_EXCLUDED_AREAS: user_input.get(CONF_EXCLUDED_AREAS, []),
                    CONF_EXCLUDED_LIGHTS: user_input.get(CONF_EXCLUDED_LIGHTS, []),
                }
            )

        schema = probatio.Schema(
            {
                probatio.Optional(CONF_EXCLUDED_AREAS): selector.AreaSelector(
                    selector.AreaSelectorConfig(multiple=True)
                ),
                probatio.Optional(CONF_EXCLUDED_LIGHTS): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=LIGHT_DOMAIN, multiple=True)
                ),
            }
        )
        return self.async_show_form(
            step_id="rooms",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )

    async def async_step_offsets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the light to set an offset for."""
        registry = er.async_get(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = registry.async_get(user_input[CONF_LIGHT])
            if (
                entry is None
                or entry.platform == DOMAIN
                or entry.platform in GROUP_PLATFORMS
            ):
                errors[CONF_LIGHT] = "invalid_light"
            else:
                # Stored by registry id, so a changed entity id keeps the offset.
                self._light = entry.id
                self._light_entity_id = entry.entity_id
                return await self.async_step_offset()

        offsets = self.config_entry.options.get(CONF_OFFSETS, {})
        current = "\n".join(
            sorted(
                f"- {er.async_resolve_entity_id(registry, registry_id) or registry_id}:"
                f" {data[CONF_OFFSET_VALUE]:+g} {data[CONF_OFFSET_UNIT]}"
                for registry_id, data in offsets.items()
            )
        )
        return self.async_show_form(
            step_id="offsets",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_LIGHT): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=LIGHT_DOMAIN)
                    )
                }
            ),
            description_placeholders={"offsets": current or "-"},
            errors=errors,
        )

    async def async_step_offset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the offset of the chosen light. An offset of 0 removes it."""
        offsets = dict(self.config_entry.options.get(CONF_OFFSETS, {}))
        errors: dict[str, str] = {}

        if user_input is not None and CONF_OFFSET_VALUE in user_input:
            unit = user_input[CONF_OFFSET_UNIT]
            value = user_input[CONF_OFFSET_VALUE]
            if abs(value) > OFFSET_LIMITS[unit]:
                errors[CONF_OFFSET_VALUE] = "offset_too_large"
            else:
                if value:
                    offsets[self._light] = {
                        CONF_OFFSET_VALUE: value,
                        CONF_OFFSET_UNIT: unit,
                    }
                else:
                    offsets.pop(self._light, None)
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_OFFSETS: offsets}
                )

        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_OFFSET_UNIT, default=UNIT_KELVIN
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[UNIT_KELVIN, UNIT_MIRED],
                        translation_key=CONF_OFFSET_UNIT,
                    )
                ),
                probatio.Required(
                    CONF_OFFSET_VALUE, default=0
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        mode=selector.NumberSelectorMode.BOX, step=1
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="offset",
            data_schema=self.add_suggested_values_to_schema(
                schema, offsets.get(self._light, {})
            ),
            description_placeholders={
                "light": self._light_entity_id,
                **LIMIT_PLACEHOLDERS,
            },
            errors=errors,
        )
