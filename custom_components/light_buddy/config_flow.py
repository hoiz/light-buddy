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
from homeassistant.helpers import selector

from .const import CONF_EXCLUDED_AREAS, CONF_EXCLUDED_LIGHTS, DOMAIN


class LightBuddyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Light Buddy. Rooms are discovered from the area registry."""

    VERSION = 1
    # 2: offset settings got English entity ids.
    MINOR_VERSION = 2

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
    """Exclude areas or single lights from Light Buddy."""

    async def async_step_init(
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
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )
