"""Send commands to the lights of a room."""

from collections import deque
from collections.abc import Iterable, Mapping
import logging
from typing import Any

from homeassistant.components.light import (
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
    color_supported,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
)
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .offset import ColorTempOffset, clamp

_LOGGER = logging.getLogger(__name__)

COLOR_ATTRS = frozenset(
    {ATTR_HS_COLOR, ATTR_RGB_COLOR, ATTR_RGBW_COLOR, ATTR_RGBWW_COLOR, ATTR_XY_COLOR}
)
COLOR_GROUP_ATTRS = COLOR_ATTRS | {ATTR_COLOR_TEMP_KELVIN, ATTR_WHITE}

# Enough to recognize the state changes our recent commands cause.
OWN_CONTEXTS = 50

type MemberParams = dict[str, dict[str, Any]]


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
            attributes = state.attributes if state is not None else {}
            params[ATTR_COLOR_TEMP_KELVIN] = clamp(
                offset.apply(params[ATTR_COLOR_TEMP_KELVIN]),
                attributes.get(ATTR_MIN_COLOR_TEMP_KELVIN),
                attributes.get(ATTR_MAX_COLOR_TEMP_KELVIN),
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
    ) -> None:
        """Initialize the commander."""
        self.hass = hass
        self._interval = interval
        self._offsets = offsets
        # Requested and sent color temperature of each light, to show the
        # requested one while the light still reports what was sent.
        self.sent_color_temps: dict[str, tuple[int, int]] = {}
        self._own_context_ids: deque[str] = deque(maxlen=OWN_CONTEXTS)
        self._next_allowed = 0.0
        self._pending: tuple[str, MemberParams, Context | None] | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None

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

        # One failing call must not keep the other lights from their command.
        first_error: Exception | None = None
        for entity_ids, call_params in calls:
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
                first_error = first_error or err
                continue
            if ATTR_COLOR_TEMP_KELVIN in call_params:
                for entity_id in entity_ids:
                    self.sent_color_temps[entity_id] = (
                        member_params[entity_id][ATTR_COLOR_TEMP_KELVIN],
                        call_params[ATTR_COLOR_TEMP_KELVIN],
                    )
        if first_error is not None:
            raise first_error


def turn_off_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return the params that turn_off accepts."""
    return {
        key: value
        for key, value in params.items()
        if key in (ATTR_TRANSITION, ATTR_FLASH)
    }
