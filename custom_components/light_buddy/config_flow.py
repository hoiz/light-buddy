"""Config and options flow for Light Buddy."""

import datetime as dt
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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .automation import ScheduleSettings, hass_sun
from .chart import MAX_POINTS, URL as CHART_URL, encode_points
from .const import (
    ANCHOR_TIME,
    CONF_ANCHOR,
    CONF_AUTO_RESUME_MINUTES,
    CONF_DISCO_INTERVAL,
    CONF_DISCO_PALETTE,
    CONF_EARLIEST,
    CONF_EXCLUDED_AREAS,
    CONF_EXCLUDED_LIGHTS,
    CONF_KELVIN,
    CONF_LATEST,
    CONF_SCHEDULE,
    CONF_SEPARATE_COMMANDS,
    CONF_SETTINGS,
    CONF_SUN_OFFSET,
    CONF_THRESHOLD,
    CONF_TIME,
    CONF_TRANSITION,
    CONF_WEEKDAYS,
    DISCO_MIN_INTERVAL,
    DOMAIN,
    SEPARATE_ALL,
    SEPARATE_IKEA,
    SEPARATE_OFF,
)
from .disco import PALETTES, DiscoSettings
from .schedule import (
    DEFAULT_SCHEDULE,
    SUN_EVENT_NAMES,
    SUN_EVENTS,
    SchedulePoint,
    format_point,
    format_table,
    point_time,
    points_from_options,
    points_to_options,
    same_slot,
    sort_points,
    today_times,
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
    # What the point being added or edited is at.
    _anchor: str = ANCHOR_TIME

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
                points_from_options(self.config_entry.options.get(CONF_SCHEDULE)),
                dt_util.now(),
                hass_sun(self.hass),
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
                "chart": f"{CHART_URL}?{urlencode({'points': encode_points(self.points), 'lang': self.hass.config.language})}",
                "points": format_table(
                    self.points,
                    self.hass.config.language,
                    dt_util.now(),
                    hass_sun(self.hass),
                ),
            },
        )

    async def async_step_add_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a point to the schedule."""
        self._editing = None
        return await self.async_step_anchor()

    async def async_step_edit_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the point to edit."""
        if user_input is not None:
            self._editing = int(user_input[CONF_POINT])
            return await self.async_step_anchor()
        return self.async_show_form(
            step_id="edit_point",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_POINT): self._point_selector(multiple=False)}
            ),
        )

    async def async_step_anchor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether a point is at a clock time or at a sun event."""
        if user_input is not None and CONF_ANCHOR in user_input:
            self._anchor = user_input[CONF_ANCHOR]
            if self._anchor == ANCHOR_TIME:
                return await self.async_step_point()
            return await self.async_step_sun_point()
        anchor = ANCHOR_TIME
        if self._editing is not None:
            anchor = self.points[self._editing].sun_event or ANCHOR_TIME
        return self.async_show_form(
            step_id="anchor",
            data_schema=self.add_suggested_values_to_schema(
                ANCHOR_SCHEMA, {CONF_ANCHOR: anchor}
            ),
        )

    async def async_step_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the time, color temperature and days of a point."""
        errors: dict[str, str] = {}
        if user_input is not None and CONF_TIME in user_input:
            if (
                result := await self._async_store_point(
                    _point_from_form(user_input), errors
                )
            ) is not None:
                return result
            suggested = user_input
        else:
            suggested = self._edited_values()
        return self.async_show_form(
            step_id="point",
            data_schema=self.add_suggested_values_to_schema(POINT_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_sun_point(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set the offset, window, color temperature and days of a sun point."""
        errors: dict[str, str] = {}
        if user_input is not None and CONF_KELVIN in user_input:
            point = _sun_point_from_form(self._anchor, user_input)
            if (
                point.earliest is not None
                and point.latest is not None
                and point.earliest > point.latest
            ):
                errors["base"] = "window_order"
            elif (result := await self._async_store_point(point, errors)) is not None:
                return result
            suggested = user_input
        else:
            suggested = self._edited_values()
        now = dt_util.now()
        event = sun_time_today(self.hass, self._anchor, now)
        return self.async_show_form(
            step_id="sun_point",
            data_schema=self.add_suggested_values_to_schema(
                SUN_POINT_SCHEMA, suggested
            ),
            errors=errors,
            description_placeholders={
                "event": SUN_EVENT_NAMES.get(
                    self.hass.config.language, SUN_EVENT_NAMES["en"]
                )[self._anchor],
                "today": event,
            },
        )

    def _edited_values(self) -> dict[str, Any]:
        """Return the form values of the point being edited, if it fits."""
        if self._editing is None:
            return {}
        point = self.points[self._editing]
        if (point.sun_event or ANCHOR_TIME) != self._anchor:
            # Changed between a clock time and a sun event: keep what fits.
            return {
                CONF_KELVIN: point.kelvin,
                CONF_WEEKDAYS: [str(day) for day in sorted(point.weekdays or ())],
            }
        return _point_form_values(point)

    async def _async_store_point(
        self, point: SchedulePoint, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Add or replace the point, and go back to the schedule.

        None with errors set if the point does not fit.
        """
        others = [
            other for index, other in enumerate(self.points) if index != self._editing
        ]
        if any(same_slot(point, other) for other in others):
            errors["base"] = "duplicate_point"
        elif len(others) >= MAX_POINTS:
            # The chart shows no more.
            errors["base"] = "too_many_points"
        else:
            self._points = sort_points(
                [*others, point], dt_util.now(), hass_sun(self.hass)
            )
            return await self.async_step_schedule()
        return None

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
                self._points = sort_points(
                    DEFAULT_SCHEDULE, dt_util.now(), hass_sun(self.hass)
                )
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
                        CONF_DISCO_INTERVAL: int(user_input[CONF_DISCO_INTERVAL]),
                        CONF_DISCO_PALETTE: user_input[CONF_DISCO_PALETTE],
                    },
                }
            )
        settings = ScheduleSettings.from_options(self.config_entry.options)
        disco = DiscoSettings.from_options(self.config_entry.options)
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                SETTINGS_SCHEMA,
                {
                    CONF_THRESHOLD: settings.threshold,
                    CONF_TRANSITION: settings.transition,
                    CONF_AUTO_RESUME_MINUTES: settings.auto_resume_minutes,
                    CONF_SEPARATE_COMMANDS: settings.separate_commands,
                    CONF_DISCO_INTERVAL: disco.interval,
                    CONF_DISCO_PALETTE: disco.palette,
                },
            ),
        )

    def _point_selector(self, *, multiple: bool) -> selector.SelectSelector:
        """Return a selector of the schedule's points."""
        language = self.hass.config.language
        times = today_times(self.points, dt_util.now(), hass_sun(self.hass))
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(
                        value=str(index),
                        label=format_point(point, language, times.get(point)),
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
        CONF_KELVIN: point.kelvin,
        CONF_WEEKDAYS: [str(day) for day in sorted(point.weekdays or ())],
    }
    if point.time is not None:
        values[CONF_TIME] = point.time.strftime("%H:%M:%S")
    else:
        values[CONF_SUN_OFFSET] = point.offset_minutes
        for key, value in (
            (CONF_EARLIEST, point.earliest),
            (CONF_LATEST, point.latest),
        ):
            if value is not None:
                values[key] = value.strftime("%H:%M:%S")
    return values


def _sun_point_from_form(anchor: str, user_input: dict[str, Any]) -> SchedulePoint:
    """Return the sun point entered in the sun point form."""
    weekdays = user_input.get(CONF_WEEKDAYS)
    return SchedulePoint(
        kelvin=int(user_input[CONF_KELVIN]),
        sun_event=anchor,
        offset_minutes=int(user_input.get(CONF_SUN_OFFSET, 0)),
        weekdays=frozenset(int(day) for day in weekdays) if weekdays else None,
        earliest=_form_time(user_input.get(CONF_EARLIEST)),
        latest=_form_time(user_input.get(CONF_LATEST)),
    )


def _form_time(value: str | None) -> dt.time | None:
    """Return a time of the form, to the minute."""
    if not value:
        return None
    return dt.time.fromisoformat(value).replace(second=0, microsecond=0)


def sun_time_today(hass: HomeAssistant, anchor: str, now: dt.datetime) -> str:
    """Return when a sun event is today, as text."""
    point = SchedulePoint(0, sun_event=anchor)
    when = point_time(point, now.date(), now.tzinfo, hass_sun(hass))
    return when.strftime("%H:%M") if when else "\u2013"


KELVIN_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=2000,
        max=6500,
        step=50,
        unit_of_measurement="K",
        mode=selector.NumberSelectorMode.SLIDER,
    )
)
WEEKDAYS_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[str(day) for day in range(7)],
        multiple=True,
        translation_key=CONF_WEEKDAYS,
    )
)

ANCHOR_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_ANCHOR): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[ANCHOR_TIME, *SUN_EVENTS],
                translation_key=CONF_ANCHOR,
                mode=selector.SelectSelectorMode.LIST,
            )
        )
    }
)

SUN_POINT_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SUN_OFFSET, default=0): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=-240,
                max=240,
                step=5,
                unit_of_measurement="min",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        probatio.Optional(CONF_EARLIEST): selector.TimeSelector(),
        probatio.Optional(CONF_LATEST): selector.TimeSelector(),
        probatio.Required(CONF_KELVIN, default=4000): KELVIN_SELECTOR,
        probatio.Optional(CONF_WEEKDAYS): WEEKDAYS_SELECTOR,
    }
)

POINT_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_TIME): selector.TimeSelector(),
        probatio.Required(CONF_KELVIN, default=4000): KELVIN_SELECTOR,
        probatio.Optional(CONF_WEEKDAYS): WEEKDAYS_SELECTOR,
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
