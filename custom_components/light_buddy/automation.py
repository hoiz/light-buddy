"""Follow the schedule in a room."""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt
import hashlib
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import (
    COLOR_TEMP_THRESHOLD_KELVIN,
    SCHEDULE_INTERVAL,
    TRANSITION_SCHEDULE,
    TRANSITION_TURN_ON,
)
from .schedule import DEFAULT_SCHEDULE, SchedulePoint, Target, target_at

if TYPE_CHECKING:
    from .light import RoomLight

_LOGGER = logging.getLogger(__name__)

# Collects lights turning on together, e.g. by a wall switch, into one command.
TURNED_ON_DEBOUNCE_SECONDS = 0.3


class RoomAutomation:
    """Keep the lights of a room on the schedule.

    Lives as long as its area, so it outlives room lights that are replaced
    when the area is renamed.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        area_id: str,
        get_light: Callable[[], RoomLight | None],
    ) -> None:
        """Initialize the automation."""
        self.hass = hass
        self.area_id = area_id
        self._get_light = get_light
        self.enabled = True
        self.points: tuple[SchedulePoint, ...] = DEFAULT_SCHEDULE
        self.target: Target | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._turned_on: set[str] = set()
        self._unsub_turned_on: CALLBACK_TYPE | None = None

    @property
    def light(self) -> RoomLight | None:
        """Return the room light, if it was added to Home Assistant."""
        light = self._get_light()
        return light if light is not None and light.hass is not None else None

    @property
    def active(self) -> bool:
        """Return True if the schedule controls the room right now."""
        return self.enabled and self.target is not None

    @callback
    def async_start(self) -> None:
        """Compute the target, and follow the schedule from now on.

        Each room is checked at its own second of the minute, so rooms do not
        send their commands at the same time.
        """
        self._async_update_target()
        offset = int(hashlib.sha256(self.area_id.encode()).hexdigest(), 16) % int(
            SCHEDULE_INTERVAL.total_seconds()
        )

        @callback
        def _async_start_interval(_now: dt.datetime) -> None:
            self._unsubs.append(
                async_track_time_interval(
                    self.hass, self._async_tick, SCHEDULE_INTERVAL
                )
            )
            self.hass.async_create_task(self._async_tick(dt_util.utcnow()))

        self._unsubs.append(async_call_later(self.hass, offset, _async_start_interval))

    @callback
    def async_stop(self) -> None:
        """Stop following the schedule."""
        while self._unsubs:
            self._unsubs.pop()()
        if self._unsub_turned_on is not None:
            self._unsub_turned_on()
            self._unsub_turned_on = None

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call update when the target or the switches change."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    async def async_set_enabled(self, enabled: bool) -> None:
        """Turn the schedule on or off, and apply it right away when on."""
        self.enabled = enabled
        self._async_notify()
        if enabled:
            await self._async_apply()

    @callback
    def async_light_turned_on(self, entity_id: str) -> None:
        """Bring a light that was turned on elsewhere to the target.

        Covers lights turned on directly, by a wall switch, or by others.
        Lights turning on together, e.g. by a wall switch, get one command.
        """
        self._turned_on.add(entity_id)
        if self._unsub_turned_on is None:
            self._unsub_turned_on = async_call_later(
                self.hass, TURNED_ON_DEBOUNCE_SECONDS, self._async_apply_turned_on
            )

    async def _async_apply_turned_on(self, _now: dt.datetime) -> None:
        self._unsub_turned_on = None
        members = list(self._turned_on)
        self._turned_on.clear()
        await self._async_apply(TRANSITION_TURN_ON, members)

    async def _async_tick(self, _now: dt.datetime) -> None:
        self._async_update_target()
        await self._async_apply()

    async def _async_apply(
        self,
        transition: float = TRANSITION_SCHEDULE,
        members: list[str] | None = None,
    ) -> None:
        if self.active and (light := self.light) is not None:
            assert self.target is not None
            try:
                await light.async_apply_target(
                    self.target,
                    transition,
                    members=members,
                    threshold=COLOR_TEMP_THRESHOLD_KELVIN,
                )
            except Exception:
                # Failed calls are logged per light, the next tick tries again.
                _LOGGER.debug("Applying the schedule in %s failed", self.area_id)

    @callback
    def _async_update_target(self) -> None:
        target = target_at(self.points, dt_util.now(), self._sun_time)
        if target != self.target:
            self.target = target
            self._async_notify()

    def _sun_time(self, event: str, day: dt.date) -> dt.datetime | None:
        return get_astral_event_date(self.hass, event, day)

    @callback
    def _async_notify(self) -> None:
        for update in list(self._listeners):
            update()
