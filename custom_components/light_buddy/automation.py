"""Follow the schedule in a room."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
import hashlib
import logging
from typing import TYPE_CHECKING, Any, Self

from homeassistant.components.light import ATTR_COLOR_NAME, ATTR_PROFILE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    COLDEST_KELVIN,
    COLOR_TEMP_THRESHOLD_KELVIN,
    CONF_AUTO_RESUME_MINUTES,
    CONF_COLDEST,
    CONF_SEPARATE_COMMANDS,
    CONF_SETTINGS,
    CONF_THRESHOLD,
    CONF_TRANSITION,
    CONF_WARMEST,
    SCHEDULE_INTERVAL,
    SEPARATE_OFF,
    TRANSITION_SCHEDULE,
    TRANSITION_TURN_ON,
    WARMEST_KELVIN,
)
from .disco import DiscoSettings, RoomDisco
from .room import COLOR_GROUP_ATTRS
from .schedule import (
    DEFAULT_SCHEDULE,
    SchedulePoint,
    Sun,
    Target,
    sun_schedule,
    target_at,
)

if TYPE_CHECKING:
    from .light import RoomLight

_LOGGER = logging.getLogger(__name__)

# Collects lights turning on together, e.g. by a wall switch, into one command.
TURNED_ON_DEBOUNCE_SECONDS = 0.3
# A pause this recent is not ended by a room that looks off.
RECENT_PAUSE_SECONDS = 10


# Also the forms of service calls, before Home Assistant converts them.
COLOR_PARAMS = COLOR_GROUP_ATTRS | {ATTR_COLOR_NAME, ATTR_PROFILE}


@dataclass(frozen=True, slots=True)
class ScheduleSettings:
    """The global settings of the schedule."""

    # Smallest change of the target sent to lights that are on, in Kelvin.
    threshold: float = COLOR_TEMP_THRESHOLD_KELVIN
    # Seconds, for the schedule's regular updates.
    transition: float = TRANSITION_SCHEDULE
    # A pause ends by itself after this many minutes, 0 keeps it until the
    # room is off.
    auto_resume_minutes: float = 0
    # Which lights get a brightness separately from the color temperature.
    separate_commands: str = SEPARATE_OFF
    # The schedule's warmest and coldest color temperature, in Kelvin.
    warmest: int = WARMEST_KELVIN
    coldest: int = COLDEST_KELVIN

    @property
    def points(self) -> tuple[SchedulePoint, ...]:
        """Return the schedule's points."""
        return sun_schedule(self.warmest, self.coldest)

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Self:
        """Return the settings stored in the options, with defaults."""
        data = options.get(CONF_SETTINGS) or {}
        defaults = cls()
        return cls(
            threshold=data.get(CONF_THRESHOLD, defaults.threshold),
            transition=data.get(CONF_TRANSITION, defaults.transition),
            auto_resume_minutes=data.get(
                CONF_AUTO_RESUME_MINUTES, defaults.auto_resume_minutes
            ),
            separate_commands=data.get(
                CONF_SEPARATE_COMMANDS, defaults.separate_commands
            ),
            warmest=int(data.get(CONF_WARMEST, defaults.warmest)),
            coldest=int(data.get(CONF_COLDEST, defaults.coldest)),
        )


def hass_sun(hass: HomeAssistant) -> Sun:
    """Return the sun at the location of Home Assistant."""
    config = hass.config
    return Sun(config.latitude, config.longitude, config.elevation)


def sets_color(params: Mapping[str, Any]) -> bool:
    """Return True if a turn on with these params sets a color on purpose.

    Colors count, too, since they take the color temperature's place.
    """
    return not COLOR_PARAMS.isdisjoint(params)


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
        enabled: bool,
        store_enabled: Callable[[str, bool], None],
    ) -> None:
        """Initialize the automation.

        Whether the schedule is on is stored in the config entry, so it holds
        even while the schedule switch is disabled.
        """
        self.hass = hass
        self.area_id = area_id
        self._get_light = get_light
        self.enabled = enabled
        self._store_enabled = store_enabled
        # Applying the schedule, by ticks and otherwise.
        self._tasks: set[asyncio.Task[None]] = set()
        self._tick: asyncio.Task[None] | None = None
        # Applies run one after another, so their commands do not interleave.
        self._apply_lock = asyncio.Lock()
        self.points: tuple[SchedulePoint, ...] = DEFAULT_SCHEDULE
        self.settings = ScheduleSettings()
        self.target: Target | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._turned_on: set[str] = set()
        self._unsub_turned_on: CALLBACK_TYPE | None = None
        # Set when the color was changed by hand, which the schedule then
        # leaves alone until the room is off or resumed.
        self.paused = False
        self.paused_since: dt.datetime | None = None
        self._paused_at = 0.0
        self._unsub_auto_resume: CALLBACK_TYPE | None = None
        self.disco = RoomDisco(self)

    @property
    def light(self) -> RoomLight | None:
        """Return the room light, if it was added to Home Assistant."""
        light = self._get_light()
        return light if light is not None and light.hass is not None else None

    @property
    def active(self) -> bool:
        """Return True if the schedule controls the room right now.

        Not during disco, which sets the colors itself.
        """
        return self.enabled and self.target is not None and not self.disco.running

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
                    self.hass, self._async_tick_later, SCHEDULE_INTERVAL
                )
            )
            self._tick = self.async_track(self._async_tick(dt_util.utcnow()))

        self._unsubs.append(async_call_later(self.hass, offset, _async_start_interval))

    @callback
    def async_stop(self) -> None:
        """Stop following the schedule."""
        while self._unsubs:
            self._unsubs.pop()()
        for task in list(self._tasks):
            task.cancel()
        if self._unsub_turned_on is not None:
            self._unsub_turned_on()
            self._unsub_turned_on = None
        self._turned_on.clear()
        self._async_cancel_auto_resume()
        self.disco.async_cancel()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call update when the target or the switches change."""
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def async_set_points(self, points: Sequence[SchedulePoint]) -> None:
        """Follow another schedule from now on."""
        self.points = tuple(points)
        self._async_update_target()

    @callback
    def async_set_disco_settings(self, settings: DiscoSettings) -> None:
        """Use other disco settings from the next color on."""
        self.disco.async_set_settings(settings)

    @callback
    def async_set_settings(self, settings: ScheduleSettings) -> None:
        """Use other settings from now on.

        A changed automatic resume applies to a running pause, counted from
        the last manual change.
        """
        changed_resume = (
            settings.auto_resume_minutes != self.settings.auto_resume_minutes
        )
        self.settings = settings
        if changed_resume and self.paused:
            self._async_start_auto_resume()

    @callback
    def async_apply_soon(self) -> None:
        """Apply the schedule now, e.g. after it was changed."""
        self.async_track(self._async_apply())

    @callback
    def async_track(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run a task that stopping the automation cancels."""
        task = self.hass.async_create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def async_set_enabled(self, enabled: bool) -> None:
        """Turn the schedule on or off, and apply it right away when on.

        Either way, a pause from changes by hand ends: turned off, the room
        stays off until turned on again, and turned on, it follows again.
        """
        self._async_clear_pause()
        self.enabled = enabled
        self._store_enabled(self.area_id, enabled)
        self.async_notify()
        if enabled:
            self._async_update_target()
            await self._async_apply()

    @callback
    def async_mark_manual(self, *, new_change: bool = True) -> None:
        """Leave the color changed by hand alone until the room is off.

        Every new change by hand starts the automatic resume over. Lights
        reporting a difference that is paused already do not, as they keep
        reporting it.
        """
        if new_change or not self.paused:
            self._paused_at = self.hass.loop.time()
            self._async_start_auto_resume()
        if self.paused:
            return
        self.paused = True
        self.paused_since = dt_util.utcnow()
        self.async_notify()

    @callback
    def _async_start_auto_resume(self) -> None:
        self._async_cancel_auto_resume()
        if self.settings.auto_resume_minutes:
            elapsed = self.hass.loop.time() - self._paused_at
            self._unsub_auto_resume = async_call_later(
                self.hass,
                max(self.settings.auto_resume_minutes * 60 - elapsed, 0),
                self._async_auto_resume_later,
            )

    async def async_resume(self) -> None:
        """Follow the schedule again in full, and apply it right away."""
        self._async_clear_pause()
        self._async_update_target()
        await self._async_apply()

    @callback
    def async_room_off(self) -> None:
        """Forget manual changes once all lights of the room are off.

        Disco ends, too, and the lights stay off. Not while disco starts,
        when the room may look off for a moment.
        """
        if self.disco.starting:
            return
        self._async_clear_pause()
        self.disco.async_room_off()

    @callback
    def _async_auto_resume_later(self, _now: dt.datetime) -> None:
        # Cleared here, a timer started before the task runs stays known.
        self._unsub_auto_resume = None
        self.async_track(self.async_resume())

    @callback
    def _async_tick_later(self, now: dt.datetime) -> None:
        # Tracked, so stopping the automation cancels a running tick. With
        # lights slow to answer, a tick still running skips the next one.
        if self._tick is not None and not self._tick.done():
            return
        self._tick = self.async_track(self._async_tick(now))

    @callback
    def _async_clear_pause(self) -> None:
        self._async_cancel_auto_resume()
        # Lights left as someone set them would otherwise count as changed
        # again, compared with what was sent before the pause.
        if (light := self.light) is not None:
            light.async_take_shown()
        if self.paused:
            self.paused = False
            self.paused_since = None
            self.async_notify()

    @callback
    def _async_cancel_auto_resume(self) -> None:
        if self._unsub_auto_resume is not None:
            self._unsub_auto_resume()
            self._unsub_auto_resume = None

    @callback
    def async_light_turned_on(self, entity_id: str) -> None:
        """Bring a light that was turned on elsewhere to the target.

        Covers lights turned on directly, by a wall switch, or by others.
        Lights turning on together, e.g. by a wall switch, get one command.
        Not during disco, whose next color reaches them.
        """
        if self.disco.running:
            return
        self._turned_on.add(entity_id)
        if self._unsub_turned_on is None:
            self._unsub_turned_on = async_call_later(
                self.hass,
                TURNED_ON_DEBOUNCE_SECONDS,
                self._async_apply_turned_on_later,
            )

    @callback
    def _async_apply_turned_on_later(self, _now: dt.datetime) -> None:
        # Taken here, lights turning on later start a new timer.
        self._unsub_turned_on = None
        members = list(self._turned_on)
        self._turned_on.clear()
        self.async_track(self._async_apply(TRANSITION_TURN_ON, members))

    async def _async_tick(self, _now: dt.datetime) -> None:
        # In case the room turned off while a turn on was still waiting. A
        # recent pause may belong to a light that is about to report on, and
        # lights that are unavailable did not turn off.
        if (
            self.paused
            and self.hass.loop.time() - self._paused_at >= RECENT_PAUSE_SECONDS
            and (light := self.light) is not None
            and light.is_on is False
            and not light.turning_on
        ):
            self.async_room_off()
        self._async_update_target()
        await self._async_apply()

    async def _async_apply(
        self,
        transition: float | None = None,
        members: list[str] | None = None,
    ) -> None:
        async with self._apply_lock:
            # While paused, the color stays as it was set by hand.
            if not self.active or self.paused or (light := self.light) is None:
                return
            assert self.target is not None
            # Lights that never answer are given up on per call.
            try:
                await light.async_apply_target(
                    self.target,
                    self.settings.transition if transition is None else transition,
                    members=members,
                    threshold=self.settings.threshold,
                )
            except Exception:
                # Failed calls are logged per light, the next tick tries again.
                _LOGGER.debug("Applying the schedule in %s failed", self.area_id)

    @callback
    def _async_update_target(self) -> None:
        target = target_at(self.points, dt_util.now(), hass_sun(self.hass))
        if target != self.target:
            self.target = target
            self.async_notify()

    @callback
    def async_notify(self) -> None:
        for update in list(self._listeners):
            update()
