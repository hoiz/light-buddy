"""Config and options flow for Light Buddy."""

import datetime as dt
from typing import Any, override

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
from .const import (
    CONF_AUTO_RESUME_MINUTES,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_KELVIN,
    CONF_SCHEDULE,
    CONF_SEPARATE_COMMANDS,
    CONF_SETTINGS,
    CONF_THRESHOLD,
    CONF_TIME,
    CONF_TRANSITION,
    CONF_WEEKDAYS,
    DOMAIN,
    SEPARATE_ALL,
    SEPARATE_IKEA,
    SEPARATE_OFF,
)
from .schedule import (
    DEFAULT_SCHEDULE,
    SchedulePoint,
    format_point,
    format_table,
    points_from_options,
    points_to_options,
    same_slot,
    sort_points,
)

CONF_POINT = "point"
CONF_POINTS = "points"
CONF_CONFIRM = "confirm"


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
    """Rooms, the schedule of all rooms, and the global settings.

    Changes to the schedule are collected and stored together when done.
    """

    _points: list[SchedulePoint] | None = None
    # The point being edited, None while adding one.
    _editing: int | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        return self.async_show_menu(
            step_id="init", menu_options=["rooms", "schedule", "settings"]
        )

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

    @property
    def points(self) -> list[SchedulePoint]:
        """Return the schedule being edited, sorted by time."""
        if self._points is None:
            self._points = sort_points(
                points_from_options(self.config_entry.options.get(CONF_SCHEDULE))
            )
        return self._points

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the schedule, and what can be done with it."""
        return self.async_show_menu(
            step_id="schedule",
            menu_options=[
                "add_point",
                "edit_point",
                "delete_points",
                "reset_schedule",
                "save_schedule",
            ],
            description_placeholders={
                "points": format_table(self.points, self.hass.config.language)
            },
        )

    async def async_step_add_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a point to the schedule."""
        self._editing = None
        return await self.async_step_point()

    async def async_step_edit_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the point to edit."""
        if user_input is not None:
            self._editing = int(user_input[CONF_POINT])
            return await self.async_step_point()
        return self.async_show_form(
            step_id="edit_point",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_POINT): self._point_selector(multiple=False)}
            ),
        )

    async def async_step_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the time, color temperature and days of a point."""
        errors: dict[str, str] = {}
        if user_input is not None and CONF_TIME in user_input:
            point = _point_from_form(user_input)
            others = [
                other
                for index, other in enumerate(self.points)
                if index != self._editing
            ]
            if any(same_slot(point, other) for other in others):
                errors["base"] = "duplicate_point"
            else:
                self._points = sort_points([*others, point])
                return await self.async_step_schedule()
            suggested = user_input
        elif self._editing is not None:
            suggested = _point_form_values(self.points[self._editing])
        else:
            suggested = {}
        return self.async_show_form(
            step_id="point",
            data_schema=self.add_suggested_values_to_schema(POINT_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_delete_points(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Delete points. At least one has to stay."""
        errors: dict[str, str] = {}
        if user_input is not None:
            delete = {int(index) for index in user_input.get(CONF_POINTS, [])}
            if len(delete) >= len(self.points):
                errors["base"] = "keep_one_point"
            else:
                self._points = [
                    point
                    for index, point in enumerate(self.points)
                    if index not in delete
                ]
                return await self.async_step_schedule()
        return self.async_show_form(
            step_id="delete_points",
            data_schema=probatio.Schema(
                {probatio.Optional(CONF_POINTS): self._point_selector(multiple=True)}
            ),
            errors=errors,
        )

    async def async_step_reset_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Go back to the default schedule, after confirming."""
        if user_input is not None:
            if user_input.get(CONF_CONFIRM):
                self._points = list(DEFAULT_SCHEDULE)
            return await self.async_step_schedule()
        return self.async_show_form(
            step_id="reset_schedule",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_CONFIRM, default=False): bool}
            ),
        )

    async def async_step_save_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Store the schedule."""
        return self.async_create_entry(
            data={
                **self.config_entry.options,
                CONF_SCHEDULE: points_to_options(self.points),
            }
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the global settings of the schedule."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_SETTINGS: {
                        CONF_THRESHOLD: int(user_input[CONF_THRESHOLD]),
                        CONF_TRANSITION: float(user_input[CONF_TRANSITION]),
                        CONF_AUTO_RESUME_MINUTES: int(
                            user_input[CONF_AUTO_RESUME_MINUTES]
                        ),
                        CONF_SEPARATE_COMMANDS: user_input[CONF_SEPARATE_COMMANDS],
                    },
                }
            )
        settings = ScheduleSettings.from_options(self.config_entry.options)
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                SETTINGS_SCHEMA,
                {
                    CONF_THRESHOLD: settings.threshold,
                    CONF_TRANSITION: settings.transition,
                    CONF_AUTO_RESUME_MINUTES: settings.auto_resume_minutes,
                    CONF_SEPARATE_COMMANDS: settings.separate_commands,
                },
            ),
        )

    def _point_selector(self, *, multiple: bool) -> selector.SelectSelector:
        """Return a selector of the schedule's points."""
        language = self.hass.config.language
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=str(index), label=format_point(point, language)
                    )
                    for index, point in enumerate(self.points)
                ],
                multiple=multiple,
                mode=selector.SelectSelectorMode.LIST,
            )
        )


def _point_from_form(user_input: dict[str, Any]) -> SchedulePoint:
    """Return the point entered in the point form."""
    weekdays = user_input.get(CONF_WEEKDAYS)
    return SchedulePoint(
        kelvin=int(user_input[CONF_KELVIN]),
        # Points are set to the minute.
        time=dt.time.fromisoformat(user_input[CONF_TIME]).replace(
            second=0, microsecond=0
        ),
        weekdays=frozenset(int(day) for day in weekdays) if weekdays else None,
    )


def _point_form_values(point: SchedulePoint) -> dict[str, Any]:
    """Return a point as the values of the point form."""
    values: dict[str, Any] = {
        CONF_TIME: point.time.strftime("%H:%M:%S") if point.time else None,
        CONF_KELVIN: point.kelvin,
        CONF_WEEKDAYS: [str(day) for day in sorted(point.weekdays or ())],
    }
    return values


POINT_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_TIME): selector.TimeSelector(),
        probatio.Required(CONF_KELVIN, default=4000): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=2000,
                max=6500,
                step=50,
                unit_of_measurement="K",
                mode=selector.NumberSelectorMode.SLIDER,
            )
        ),
        probatio.Optional(CONF_WEEKDAYS): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[str(day) for day in range(7)],
                multiple=True,
                translation_key=CONF_WEEKDAYS,
            )
        ),
    }
)

SETTINGS_SCHEMA = probatio.Schema(
    {
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
    }
)
