"""Changing colors in a room."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any, Self

from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN
from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_DISCO_INTERVAL,
    CONF_DISCO_PALETTE,
    CONF_SETTINGS,
    DISCO_INTERVAL,
    DISCO_PALETTE,
    DOMAIN,
)

if TYPE_CHECKING:
    from .automation import RoomAutomation

_LOGGER = logging.getLogger(__name__)

# A room turning off this soon after disco started may be lights switching
# over, white ones turning off before the colored ones report on.
START_GRACE_SECONDS = 5
# The fade into each color. IKEA lights ignore commands during a transition,
# so it is kept short, and waited for before disco ends.
DISCO_FADE_SECONDS = 1
FADE_PAUSE_SECONDS = 0.5

type HsColor = tuple[float, float]

PALETTES: dict[str, tuple[HsColor, ...]] = {
    "rainbow": (
        (0, 100),
        (30, 100),
        (55, 100),
        (120, 100),
        (180, 100),
        (240, 100),
        (280, 100),
    ),
    "party": ((300, 100), (270, 100), (230, 100), (330, 90), (190, 100)),
    "fire": ((0, 100), (15, 100), (30, 100), (40, 100), (50, 90)),
    "ocean": ((240, 100), (220, 100), (190, 100), (165, 100), (260, 90)),
}


@dataclass(frozen=True, slots=True)
class DiscoSettings:
    """The global settings of the disco mode."""

    # Seconds between two color changes.
    interval: float = DISCO_INTERVAL
    palette: str = DISCO_PALETTE

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Self:
        """Return the settings stored in the options, with defaults."""
        data = options.get(CONF_SETTINGS) or {}
        defaults = cls()
        palette = data.get(CONF_DISCO_PALETTE, defaults.palette)
        return cls(
            interval=data.get(CONF_DISCO_INTERVAL, defaults.interval),
            palette=palette if palette in PALETTES else defaults.palette,
        )


def step_colors(
    lights: Sequence[str], palette: Sequence[HsColor], step: int
) -> dict[str, HsColor]:
    """Return the color of each light in a step.

    The palette moves on by one color per step, so neighbors differ.
    """
    return {
        entity_id: palette[(index + step) % len(palette)]
        for index, entity_id in enumerate(lights)
    }


class RoomDisco:
    """Let the colored lights of a room change colors.

    Lives as long as the room's automation. The lights' state from before
    is restored when disco is turned off.
    """

    def __init__(self, automation: RoomAutomation) -> None:
        """Initialize the disco mode."""
        self._automation = automation
        self.hass = automation.hass
        self.settings = DiscoSettings()
        self.running = False
        self._step = 0
        self._started_at = 0.0
        # The colored lights, in the order of their colors, fixed at the start
        # so colors do not jump when lights come and go.
        self._lights: list[str] = []
        # The params each light gets back, None for lights to turn off.
        self._snapshot: dict[str, dict[str, Any] | None] = {}
        # Lights switched by others against disco: colored ones off, white
        # ones on. They keep that, and are not switched back.
        self._switched: set[str] = set()
        # Until when, by loop time, lights fade into the last color.
        self._fading_until = 0.0
        self._unsub_step: CALLBACK_TYPE | None = None
        # The start or step that is sending.
        self._task: asyncio.Task[None] | None = None
        # The stop waiting for commands and fades to end before it restores,
        # and what it restores.
        self._stopping: asyncio.Task[None] | None = None
        self._restore = False
        # Disco was turned on again before the restore went out.
        self._resumed = False
        # Counts stops, so a start waiting for one notices a later one.
        self._generation = 0
        self._stopping_snapshot: dict[str, dict[str, Any] | None] = {}
        self._stopping_switched: set[str] = set()

    @property
    def starting(self) -> bool:
        """Return True right after the start, while lights switch over.

        White lights may report off before the colored ones report on, so the
        room may look off for a moment.
        """
        return (
            self.running
            and self.hass.loop.time() - self._started_at < START_GRACE_SECONDS
        )

    async def async_start(self) -> None:
        """Start changing colors, after taking what the lights show.

        Turned on again before a restore went out, the restore is dropped and
        what was taken before stays, as the lights still show disco.
        """
        snapshot: dict[str, dict[str, Any] | None] | None = None
        switched: set[str] = set()
        if (stopping := self._stopping) is not None:
            if self._restore:
                snapshot, switched = self._stopping_snapshot, self._stopping_switched
            self._resumed = True
            generation = self._generation
            await asyncio.wait([stopping])
            # Stopped again meanwhile, or unloaded: the latest wins.
            if self._generation != generation:
                return
        if self.running:
            return
        if (light := self._automation.light) is None or not (
            lights := light.color_members
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_color_lights"
            )
        self._snapshot = light.async_snapshot() if snapshot is None else snapshot
        self._lights = lights
        self._switched = switched
        self.running = True
        self._step = 0
        self._started_at = self.hass.loop.time()
        self._fading_until = 0.0
        self._automation.async_notify()
        self._async_schedule_step()
        self._task = self._automation.async_track(
            self._async_send(
                light.async_disco_start(
                    step_colors(lights, PALETTES[self.settings.palette], 0)
                )
            )
        )
        # Not cancelled along with the task, when disco is stopped meanwhile.
        await asyncio.wait([self._task])

    async def async_stop(self, *, restore: bool) -> None:
        """Stop changing colors, and restore what the lights showed before.

        Commands still being sent are cancelled, and a fade still running is
        waited for, as IKEA lights ignore commands during it. A stop without
        restore, e.g. turning the room off, drops a restore still waiting.
        """
        # A start waiting for a stop gives up.
        self._generation += 1
        self._resumed = False
        if self._stopping is None:
            if not self.running:
                return
            self._restore = restore
            self._async_begin_stop()
        elif not restore:
            self._restore = False
        if (stopping := self._stopping) is not None:
            await asyncio.wait([stopping])

    @callback
    def _async_begin_stop(self) -> None:
        self.running = False
        self._async_cancel_step()
        self._stopping_snapshot, self._snapshot = self._snapshot, {}
        self._stopping_switched, self._switched = self._switched, set()
        self._automation.async_notify()
        self._stopping = self._automation.async_track(self._async_stop())

    async def _async_stop(self) -> None:
        try:
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                await asyncio.wait([task])
            if (delay := self._fading_until - self.hass.loop.time()) > 0:
                await asyncio.sleep(delay + FADE_PAUSE_SECONDS)
            if (
                self._restore
                and not self._resumed
                and (light := self._automation.light) is not None
            ):
                await light.async_restore(
                    self._restorable(self._stopping_snapshot),
                    self._restored_color_temp(),
                )
        finally:
            self._stopping = None
            self._resumed = False
            self._stopping_snapshot = {}
            self._stopping_switched = set()

    def _restorable(
        self, snapshot: dict[str, dict[str, Any] | None]
    ) -> dict[str, dict[str, Any] | None]:
        """Leave out lights someone switched against disco, and keep it so.

        Checked against what they show now, as lights may report the switches
        of disco itself late.
        """
        snapshot = dict(snapshot)
        for entity_id in self._stopping_switched:
            state = self.hass.states.get(entity_id)
            on = state is not None and state.state == STATE_ON
            if entity_id in self._lights:
                # A colored light turned off stays off.
                if not on:
                    snapshot.pop(entity_id, None)
            elif on and snapshot.get(entity_id) is None:
                # A white light turned on that was off before stays on.
                snapshot.pop(entity_id, None)
        return snapshot

    @callback
    def async_room_off(self) -> None:
        """Stop without restoring once the room is off."""
        if self.running and not self.starting:
            self._automation.async_track(self.async_stop(restore=False))

    @callback
    def async_light_switched(self, entity_id: str, on: bool) -> None:
        """Note a light switched by others, which may then keep its state."""
        if self.running and on != (entity_id in self._lights):
            self._switched.add(entity_id)

    @callback
    def async_set_settings(self, settings: DiscoSettings) -> None:
        """Use other settings from the next step on."""
        changed_interval = settings.interval != self.settings.interval
        self.settings = settings
        if changed_interval and self.running:
            self._async_schedule_step()

    @callback
    def async_cancel(self) -> None:
        """Stop at once, e.g. when the automation stops. Nothing is restored."""
        self._generation += 1
        self.running = False
        self._async_cancel_step()
        for task in (self._task, self._stopping):
            if task is not None:
                task.cancel()
        self._task = self._stopping = None
        self._snapshot = {}

    def _restored_color_temp(self) -> float | None:
        """Return the color temperature lights following the schedule get."""
        automation = self._automation
        if automation.active and not automation.paused and automation.target:
            return automation.target.kelvin
        return None

    @callback
    def _async_schedule_step(self) -> None:
        self._async_cancel_step()
        self._unsub_step = async_call_later(
            self.hass, self.settings.interval, self._async_step_later
        )

    @callback
    def _async_cancel_step(self) -> None:
        if self._unsub_step is not None:
            self._unsub_step()
            self._unsub_step = None

    @callback
    def _async_step_later(self, _now: dt.datetime) -> None:
        self._unsub_step = None
        if not self.running:
            return
        self._async_schedule_step()
        # With lights slow to answer, a start or step still sending skips the
        # next step.
        if self._task is None or self._task.done():
            self._task = self._automation.async_track(self._async_step())

    async def _async_step(self) -> None:
        if (light := self._automation.light) is None:
            return
        if light.is_on is False:
            # Turned off right after starting, within the grace. Lights slow
            # to report on get until the grace is over. The stop cancels this
            # step, so it is not waited for here.
            if not self.starting:
                self._restore = False
                self._async_begin_stop()
            return
        self._step += 1
        # Short, so lights are rarely busy fading when other commands come.
        transition = min(DISCO_FADE_SECONDS, self.settings.interval)
        self._fading_until = self.hass.loop.time() + transition
        await self._async_send(
            light.async_disco_step(
                step_colors(self._lights, PALETTES[self.settings.palette], self._step),
                transition,
            )
        )
        # Lights slow to answer delay the later calls, and their fades.
        self._fading_until = self.hass.loop.time() + transition

    async def _async_send(self, coro: Coroutine[Any, Any, None]) -> None:
        try:
            await coro
        except Exception:
            # Failed calls are logged per light, the next step tries again.
            _LOGGER.debug("Sending disco colors in %s failed", self._automation.area_id)


def restored_params(params: dict[str, Any], color_temp: float | None) -> dict[str, Any]:
    """Return the params a light gets back, following the schedule if given."""
    if color_temp is not None and ATTR_COLOR_TEMP_KELVIN in params:
        return {**params, ATTR_COLOR_TEMP_KELVIN: color_temp}
    return params
