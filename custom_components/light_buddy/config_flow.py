"""Config and options flow for Light Buddy."""

from typing import Any, override
from urllib.parse import urlencode

import probatio

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .automation import ScheduleSettings
from .chart import URL as CHART_URL, encode_points
from .const import (
    CONF_AUTO_RESUME_MINUTES,
    CONF_COLDEST,
    CONF_DISCO_INTERVAL,
    CONF_DISCO_PALETTE,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_SEPARATE_COMMANDS,
    CONF_SETTINGS,
    CONF_THRESHOLD,
    CONF_TRANSITION,
    CONF_WARMEST,
    DISCO_MIN_INTERVAL,
    DOMAIN,
    MIN_SCHEDULE_SPAN_KELVIN,
    SEPARATE_ALL,
    SEPARATE_IKEA,
    SEPARATE_OFF,
)
from .disco import PALETTES, DiscoSettings

# Where schedules of points were stored, before the schedule was fixed.
OLD_CONF_SCHEDULE = "schedule"


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


class LightBuddyOptionsFlow(OptionsFlow):
    """Rooms, and the global settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(step_id="init", menu_options=["rooms", "settings"])

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

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the global settings, with a chart of the saved schedule.

        A schedule stored before it was fixed is dropped.
        """
        errors: dict[str, str] = {}
        if user_input is not None and (
            user_input[CONF_COLDEST] - user_input[CONF_WARMEST]
            < MIN_SCHEDULE_SPAN_KELVIN
        ):
            errors["base"] = "schedule_span"
        elif user_input is not None:
            options = {
                key: value
                for key, value in self.config_entry.options.items()
                if key != OLD_CONF_SCHEDULE
            }
            return self.async_create_entry(
                data={
                    **options,
                    CONF_SETTINGS: {
                        CONF_WARMEST: int(user_input[CONF_WARMEST]),
                        CONF_COLDEST: int(user_input[CONF_COLDEST]),
                        CONF_THRESHOLD: int(user_input[CONF_THRESHOLD]),
                        CONF_TRANSITION: float(user_input[CONF_TRANSITION]),
                        CONF_AUTO_RESUME_MINUTES: int(
                            user_input[CONF_AUTO_RESUME_MINUTES]
                        ),
                        CONF_SEPARATE_COMMANDS: user_input[CONF_SEPARATE_COMMANDS],
                        CONF_DISCO_INTERVAL: int(user_input[CONF_DISCO_INTERVAL]),
                        CONF_DISCO_PALETTE: user_input[CONF_DISCO_PALETTE],
                    },
                }
            )
        settings = ScheduleSettings.from_options(self.config_entry.options)
        disco = DiscoSettings.from_options(self.config_entry.options)
        query = urlencode(
            {
                "points": encode_points(settings.points),
                "lang": self.hass.config.language,
            }
        )
        return self.async_show_form(
            step_id="settings",
            errors=errors,
            description_placeholders={"chart": f"{CHART_URL}?{query}"},
            data_schema=self.add_suggested_values_to_schema(
                SETTINGS_SCHEMA,
                user_input
                or {
                    CONF_WARMEST: settings.warmest,
                    CONF_COLDEST: settings.coldest,
                    CONF_THRESHOLD: settings.threshold,
                    CONF_TRANSITION: settings.transition,
                    CONF_AUTO_RESUME_MINUTES: settings.auto_resume_minutes,
                    CONF_SEPARATE_COMMANDS: settings.separate_commands,
                    CONF_DISCO_INTERVAL: disco.interval,
                    CONF_DISCO_PALETTE: disco.palette,
                },
            ),
        )


SETTINGS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_WARMEST): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=2000,
                max=4000,
                step=50,
                unit_of_measurement="K",
                mode=selector.NumberSelectorMode.SLIDER,
            )
        ),
        probatio.Required(CONF_COLDEST): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=3500,
                max=6500,
                step=50,
                unit_of_measurement="K",
                mode=selector.NumberSelectorMode.SLIDER,
            )
        ),
        probatio.Required(CONF_THRESHOLD): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=10,
                max=500,
                step=10,
                unit_of_measurement="K",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        probatio.Required(CONF_TRANSITION): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=10,
                step=0.5,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        probatio.Required(CONF_AUTO_RESUME_MINUTES): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=720,
                step=5,
                unit_of_measurement="min",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        probatio.Required(CONF_SEPARATE_COMMANDS): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[SEPARATE_OFF, SEPARATE_IKEA, SEPARATE_ALL],
                translation_key=CONF_SEPARATE_COMMANDS,
            )
        ),
        probatio.Required(CONF_DISCO_INTERVAL): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=DISCO_MIN_INTERVAL,
                max=60,
                step=1,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        probatio.Required(CONF_DISCO_PALETTE): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(PALETTES), translation_key=CONF_DISCO_PALETTE
            )
        ),
    }
)
