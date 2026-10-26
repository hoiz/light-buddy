"""Send commands to the lights of a room."""

import asyncio
from collections import deque
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass
import datetime as dt
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_FLASH,
    ATTR_HS_COLOR,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_SUPPORTED_COLOR_MODES,
    ATTR_TRANSITION,
    ATTR_WHITE,
    ATTR_XY_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
    color_supported,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import SEPARATE_COMMANDS_PAUSE_SECONDS
from .offset import ColorTempOffset, clamp

_LOGGER = logging.getLogger(__name__)

COLOR_ATTRS = frozenset(
    {ATTR_HS_COLOR, ATTR_RGB_COLOR, ATTR_RGBW_COLOR, ATTR_RGBWW_COLOR, ATTR_XY_COLOR}
)
COLOR_GROUP_ATTRS = COLOR_ATTRS | {ATTR_COLOR_TEMP_KELVIN, ATTR_WHITE}
# Descriptors that replace a color temperature on a light.
NOT_COLOR_TEMP_ATTRS = COLOR_GROUP_ATTRS - {ATTR_COLOR_TEMP_KELVIN}

# Enough to recognize the state changes our recent commands cause.
OWN_CONTEXTS = 50

type MemberParams = dict[str, dict[str, Any]]

# Values remembered per light, to tell changes made elsewhere.
TRACKED_ATTRS = (ATTR_COLOR_TEMP_KELVIN, ATTR_BRIGHTNESS)


@dataclass(frozen=True, slots=True)
class SentValue:
    """A value a light got, and when it should have reached it."""

    value: float
    sent_at: float
    transition: float = 0

    def settled(self, now: float, grace: float) -> bool:
        """Return True once the light should show the value."""
        return now - self.sent_at >= self.transition + grace


def reachable_kelvin(kelvin: float, attributes: Mapping[str, Any]) -> int:
    """Return what a light can show of a color temperature, within its range."""
    return clamp(
        kelvin,
        attributes.get(ATTR_MIN_COLOR_TEMP_KELVIN),
        attributes.get(ATTR_MAX_COLOR_TEMP_KELVIN),
    )


@callback
def async_split_turn_on(
    hass: HomeAssistant,
    member_params: Mapping[str, dict[str, Any]],
    offsets: Mapping[str, ColorTempOffset],
) -> list[tuple[list[str], dict[str, Any]]]:
    """Turn the params of each light into as few calls as possible.

    Lights that receive identical params share one service call. Colors only go
    to lights that support color, because Home Assistant would otherwise turn a
    color into some color temperature on white lights. Lights with an offset
    get their own color temperature.
    """
    calls: list[tuple[list[str], dict[str, Any]]] = []
    for entity_id, requested in member_params.items():
        params = dict(requested)
        state = hass.states.get(entity_id)
        if (
            ATTR_COLOR_TEMP_KELVIN in params
            and (offset := offsets.get(entity_id)) is not None
        ):
            params[ATTR_COLOR_TEMP_KELVIN] = reachable_kelvin(
                offset.apply(params[ATTR_COLOR_TEMP_KELVIN]),
                state.attributes if state is not None else {},
            )
        if state is not None:
            if not color_supported(state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)):
                for attr in COLOR_ATTRS:
                    params.pop(attr, None)
            if ATTR_EFFECT in params and params[ATTR_EFFECT] not in (
                state.attributes.get(ATTR_EFFECT_LIST) or ()
            ):
                del params[ATTR_EFFECT]
        for entity_ids, call_params in calls:
            if call_params == params:
                entity_ids.append(entity_id)
                break
        else:
            calls.append(([entity_id], params))
    return calls


@dataclass(slots=True)
class WaitingBrightness:
    """Brightness to send once the color sent before it is done.

    Newer commands take their lights out of entity_ids, while it waits.
    """

    calls: list[tuple[list[str], dict[str, Any]]]
    entity_ids: set[str]
    # When the color was sent, to tell lights turned off since.
    sent_at: dt.datetime


def split_separate(
    calls: list[tuple[list[str], dict[str, Any]]], separate: Container[str]
) -> tuple[
    list[tuple[list[str], dict[str, Any]]], list[tuple[list[str], dict[str, Any]]]
]:
    """Split calls with brightness and color for lights that need it.

    Some lights, IKEA ones in particular, do not change brightness and color
    together cleanly. They get the color first, so lights turning on show the
    right color, and the brightness later. Each gets half of the transition,
    so the light is busy no longer than with one command. Returns the calls to
    send now and the ones to send later.
    """
    now: list[tuple[list[str], dict[str, Any]]] = []
    later: list[tuple[list[str], dict[str, Any]]] = []
    for entity_ids, params in calls:
        separated = [entity_id for entity_id in entity_ids if entity_id in separate]
        if (
            not separated
            or ATTR_BRIGHTNESS not in params
            or COLOR_GROUP_ATTRS.isdisjoint(params)
        ):
            now.append((entity_ids, params))
            continue
        if together := [e for e in entity_ids if e not in separate]:
            now.append((together, params))
        color = {key: value for key, value in params.items() if key != ATTR_BRIGHTNESS}
        brightness = {ATTR_BRIGHTNESS: params[ATTR_BRIGHTNESS]}
        if ATTR_TRANSITION in params:
            color[ATTR_TRANSITION] = brightness[ATTR_TRANSITION] = (
                params[ATTR_TRANSITION] / 2
            )
        now.append((separated, color))
        later.append((separated, brightness))
    return now, later


def merge_params(older: dict[str, Any], newer: dict[str, Any]) -> dict[str, Any]:
    """Merge two turn_on params, the newer ones win."""
    # Home Assistant accepts only one color descriptor per call.
    if not COLOR_GROUP_ATTRS.isdisjoint(newer):
        older = {
            key: value for key, value in older.items() if key not in COLOR_GROUP_ATTRS
        }
    return {**older, **newer}


class RoomCommander:
    """Forward commands to the lights of a room, at most one per interval.

    Commands arriving faster than the interval, like while dragging a
    brightness slider, are merged and only the latest values are sent.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        interval: float,
        offsets: Mapping[str, ColorTempOffset],
        separate: Container[str] = frozenset(),
    ) -> None:
        """Initialize the commander.

        Lights in separate get brightness and color as separate commands.
        """
        self.hass = hass
        self._interval = interval
        self._offsets = offsets
        self._separate = separate
        # Brightness commands waiting for their color command to finish.
        self._later: dict[asyncio.Task[None], WaitingBrightness] = {}
        # Requested and sent color temperature of each light, to show the
        # requested one while the light still reports what was sent.
        self.sent_color_temps: dict[str, tuple[int, int]] = {}
        self._own_context_ids: deque[str] = deque(maxlen=OWN_CONTEXTS)
        self._own_turn_off_ids: deque[str] = deque(maxlen=OWN_CONTEXTS)
        # The last color temperature and brightness each light got, to tell
        # later changes made elsewhere.
        self.last_sent: dict[str, dict[str, SentValue]] = {}
        self._next_allowed = 0.0
        self._pending: tuple[str, MemberParams, Context | None] | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None

    @property
    def turning_on(self) -> bool:
        """Return True if a turn on waits to be sent."""
        return self._pending is not None and self._pending[0] == SERVICE_TURN_ON

    @callback
    def async_forget_sent(self, entity_ids: Iterable[str]) -> None:
        """Forget what was sent, so later changes are not compared with it."""
        for entity_id in entity_ids:
            self.last_sent.pop(entity_id, None)

    @callback
    def async_take_shown(self, entity_ids: Iterable[str]) -> None:
        """Compare later changes with what the lights show now.

        For lights left as someone set them, e.g. when a pause ends.
        """
        now = self.hass.loop.time()
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            # Lights showing a color are not controlled by the schedule.
            if (
                state is None
                or state.state != STATE_ON
                or state.attributes.get(ATTR_COLOR_MODE) != ColorMode.COLOR_TEMP
            ):
                self.last_sent.pop(entity_id, None)
                continue
            sent = self.last_sent.get(entity_id, {})
            shown = {}
            for attr in TRACKED_ATTRS:
                if (value := sent.get(attr)) is not None and not value.settled(now, 0):
                    # Still fading towards it, keep the fade.
                    shown[attr] = value
                elif (current := state.attributes.get(attr)) is not None:
                    shown[attr] = SentValue(current, now)
            self.last_sent[entity_id] = shown

    @property
    def turning_off(self) -> bool:
        """Return True if a turn off waits to be sent."""
        return self._pending is not None and self._pending[0] == SERVICE_TURN_OFF

    def is_own_context(self, context: Context) -> bool:
        """Return True if a state change was caused by one of our commands."""
        return context.id in self._own_context_ids or (
            context.parent_id is not None and context.parent_id in self._own_context_ids
        )

    async def async_turn_on(
        self,
        members: Iterable[str],
        params: dict[str, Any],
        context: Context | None,
        overrides: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        """Turn on the given lights, some of them with additional params."""
        overrides = overrides or {}
        await self._async_submit(
            SERVICE_TURN_ON,
            {
                entity_id: {**params, **overrides.get(entity_id, {})}
                for entity_id in members
            },
            context,
        )

    async def async_turn_off(
        self, members: Iterable[str], params: dict[str, Any], context: Context | None
    ) -> None:
        """Turn off the given lights."""
        await self._async_submit(
            SERVICE_TURN_OFF, dict.fromkeys(members, params), context
        )

    async def async_send_now(self, member_params: MemberParams) -> None:
        """Turn on lights at once with their own params, apart from throttling.

        For commands of Light Buddy itself, which must not be merged with the
        room's commands.
        """
        await self._async_send(SERVICE_TURN_ON, member_params, Context())

    @callback
    def async_cancel(self) -> None:
        """Drop pending commands."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        self._pending = None
        for task in list(self._later):
            task.cancel()

    async def _async_submit(
        self, service: str, member_params: MemberParams, context: Context | None
    ) -> None:
        now = self.hass.loop.time()
        if self._unsub_timer is None and now >= self._next_allowed:
            self._next_allowed = now + self._interval
            await self._async_send(service, member_params, context)
            return

        if self._pending is not None and service == self._pending[0] == SERVICE_TURN_ON:
            # Lights only in the older command still need it.
            older = self._pending[1]
            member_params = {
                **older,
                **{
                    entity_id: merge_params(older.get(entity_id, {}), params)
                    for entity_id, params in member_params.items()
                },
            }
        self._pending = (service, member_params, context)
        if self._unsub_timer is None:
            self._unsub_timer = async_call_later(
                self.hass, max(self._next_allowed - now, 0), self._async_flush
            )

    async def _async_flush(self, _now: Any) -> None:
        self._unsub_timer = None
        if self._pending is None:
            return
        service, member_params, context = self._pending
        self._pending = None
        self._next_allowed = self.hass.loop.time() + self._interval
        await self._async_send(service, member_params, context)

    async def _async_send(
        self, service: str, member_params: MemberParams, context: Context | None
    ) -> None:
        if context is None:
            context = Context()
        self._own_context_ids.append(context.id)
        if service == SERVICE_TURN_OFF:
            self._own_turn_off_ids.append(context.id)
        # A newer brightness, or a turn off, replaces brightness still waiting
        # for the same lights. Other lights of that command keep waiting.
        replaced = {
            entity_id
            for entity_id, params in member_params.items()
            if service == SERVICE_TURN_OFF or ATTR_BRIGHTNESS in params
        }
        for task, waiting in list(self._later.items()):
            waiting.entity_ids -= replaced
            if not waiting.entity_ids:
                task.cancel()

        if service == SERVICE_TURN_OFF:
            # Checked when sending, so lights a pending turn_on just switched
            # on are included.
            members = [
                entity_id
                for entity_id in member_params
                if (state := self.hass.states.get(entity_id)) is None
                or state.state != STATE_OFF
            ]
            params = next(iter(member_params.values()), {})
            calls = [(members, params)] if members else []
        else:
            calls = async_split_turn_on(self.hass, member_params, self._offsets)

        later: list[tuple[list[str], dict[str, Any]]] = []
        if service == SERVICE_TURN_ON:
            calls, later = split_separate(calls, self._separate)

        # One failing call must not keep the other lights from their command.
        sent_at = dt_util.utcnow()
        first_error: Exception | None = None
        failed: set[str] = set()
        for entity_ids, call_params in calls:
            error = await self._async_call(
                service, entity_ids, call_params, member_params, context
            )
            if error is not None:
                first_error = first_error or error
                failed.update(entity_ids)
        if later:
            # Not waited for, so the caller is not held up by the pause.
            # After the color's half of the transition.
            delay = (
                max(params.get(ATTR_TRANSITION, 0) for _, params in later)
                + SEPARATE_COMMANDS_PAUSE_SECONDS
            )
            # Lights whose color failed get no brightness on its own either.
            waiting = WaitingBrightness(
                later,
                {
                    entity_id
                    for entity_ids, _ in later
                    for entity_id in entity_ids
                    if entity_id not in failed
                },
                sent_at,
            )
            if waiting.entity_ids:
                task = self.hass.async_create_background_task(
                    self._async_send_later(waiting, delay, member_params, context),
                    "light_buddy_separate_commands",
                )
                self._later[task] = waiting
                task.add_done_callback(lambda task: self._later.pop(task, None))
        if first_error is not None:
            raise first_error

    async def _async_send_later(
        self,
        waiting: WaitingBrightness,
        delay: float,
        member_params: MemberParams,
        context: Context,
    ) -> None:
        await asyncio.sleep(delay)
        for entity_ids, call_params in waiting.calls:
            # Lights turned off by others since the color was sent, e.g. by a
            # wall switch, stay off. Lights that did not report on yet still
            # get it, as do lights reporting the end of our own turn off. Not
            # any context of ours: lights keep that of the color a while.
            if send := [
                entity_id
                for entity_id in entity_ids
                if entity_id in waiting.entity_ids
                and not (
                    (state := self.hass.states.get(entity_id)) is not None
                    and state.state != STATE_ON
                    and state.last_changed >= waiting.sent_at
                    and state.context.id not in self._own_turn_off_ids
                )
            ]:
                await self._async_call(
                    SERVICE_TURN_ON, send, call_params, member_params, context
                )

    async def _async_call(
        self,
        service: str,
        entity_ids: list[str],
        call_params: dict[str, Any],
        member_params: MemberParams,
        context: Context,
    ) -> Exception | None:
        """Send one service call and remember what it sent, or the error."""
        try:
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                service,
                {**call_params, ATTR_ENTITY_ID: entity_ids},
                blocking=True,
                context=context,
            )
        except Exception as err:
            _LOGGER.warning("Sending %s to %s failed: %s", service, entity_ids, err)
            return err
        if service == SERVICE_TURN_ON:
            self._async_remember_sent(entity_ids, call_params)
        if ATTR_COLOR_TEMP_KELVIN in call_params:
            for entity_id in entity_ids:
                self.sent_color_temps[entity_id] = (
                    member_params[entity_id][ATTR_COLOR_TEMP_KELVIN],
                    call_params[ATTR_COLOR_TEMP_KELVIN],
                )
        return None

    @callback
    def _async_remember_sent(
        self, entity_ids: list[str], call_params: dict[str, Any]
    ) -> None:
        """Remember per attribute what lights got, keeping the others."""
        now = self.hass.loop.time()
        transition = call_params.get(ATTR_TRANSITION, 0)
        for entity_id in entity_ids:
            sent = self.last_sent.setdefault(entity_id, {})
            if not NOT_COLOR_TEMP_ATTRS.isdisjoint(call_params):
                # Showing a color now, no color temperature to compare.
                sent.pop(ATTR_COLOR_TEMP_KELVIN, None)
            for attr in TRACKED_ATTRS:
                if attr in call_params:
                    sent[attr] = SentValue(call_params[attr], now, transition)


def turn_off_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return the params that turn_off accepts."""
    return {
        key: value
        for key, value in params.items()
        if key in (ATTR_TRANSITION, ATTR_FLASH)
    }
