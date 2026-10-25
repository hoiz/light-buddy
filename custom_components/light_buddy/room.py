"""Send commands to the lights of a room."""

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


@callback
def async_split_turn_on(
    hass: HomeAssistant,
    members: Iterable[str],
    params: dict[str, Any],
    offsets: Mapping[str, ColorTempOffset],
) -> list[tuple[list[str], dict[str, Any]]]:
    """Split turn_on params into as few calls as possible.

    Lights that receive identical params share one service call. Colors only go
    to lights that support color, because Home Assistant would otherwise turn a
    color into some color temperature on white lights. Lights with an offset
    get their own color temperature.
    """
    calls: list[tuple[list[str], dict[str, Any]]] = []
    for entity_id in members:
        member_params = dict(params)
        state = hass.states.get(entity_id)
        if ATTR_COLOR_TEMP_KELVIN in params and (offset := offsets.get(entity_id)):
            attributes = state.attributes if state is not None else {}
            member_params[ATTR_COLOR_TEMP_KELVIN] = clamp(
                offset.apply(params[ATTR_COLOR_TEMP_KELVIN]),
                attributes.get(ATTR_MIN_COLOR_TEMP_KELVIN),
                attributes.get(ATTR_MAX_COLOR_TEMP_KELVIN),
            )
        if state is not None:
            if not color_supported(state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)):
                for attr in COLOR_ATTRS:
                    member_params.pop(attr, None)
            if ATTR_EFFECT in member_params and member_params[ATTR_EFFECT] not in (
                state.attributes.get(ATTR_EFFECT_LIST) or ()
            ):
                del member_params[ATTR_EFFECT]
        for entity_ids, call_params in calls:
            if call_params == member_params:
                entity_ids.append(entity_id)
                break
        else:
            calls.append(([entity_id], member_params))
    return calls


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
        self._next_allowed = 0.0
        self._pending: tuple[str, list[str], dict[str, Any], Context | None] | None = (
            None
        )
        self._unsub_timer: CALLBACK_TYPE | None = None

    async def async_turn_on(
        self, members: list[str], params: dict[str, Any], context: Context | None
    ) -> None:
        """Turn on the given lights."""
        await self._async_submit(SERVICE_TURN_ON, members, params, context)

    async def async_turn_off(
        self, members: list[str], params: dict[str, Any], context: Context | None
    ) -> None:
        """Turn off the given lights."""
        await self._async_submit(SERVICE_TURN_OFF, members, params, context)

    @callback
    def async_cancel(self) -> None:
        """Drop pending commands."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
        self._pending = None

    async def _async_submit(
        self,
        service: str,
        members: list[str],
        params: dict[str, Any],
        context: Context | None,
    ) -> None:
        now = self.hass.loop.time()
        if self._unsub_timer is None and now >= self._next_allowed:
            self._next_allowed = now + self._interval
            await self._async_send(service, members, params, context)
            return

        if (
            self._pending is not None
            and service == SERVICE_TURN_ON
            and self._pending[0] == SERVICE_TURN_ON
        ):
            older = self._pending[2]
            # Home Assistant accepts only one color descriptor per call.
            if not COLOR_GROUP_ATTRS.isdisjoint(params):
                older = {
                    key: value
                    for key, value in older.items()
                    if key not in COLOR_GROUP_ATTRS
                }
            params = {**older, **params}
        self._pending = (service, members, params, context)
        if self._unsub_timer is None:
            self._unsub_timer = async_call_later(
                self.hass, max(self._next_allowed - now, 0), self._async_flush
            )

    async def _async_flush(self, _now: Any) -> None:
        self._unsub_timer = None
        if self._pending is None:
            return
        service, members, params, context = self._pending
        self._pending = None
        self._next_allowed = self.hass.loop.time() + self._interval
        await self._async_send(service, members, params, context)

    async def _async_send(
        self,
        service: str,
        members: list[str],
        params: dict[str, Any],
        context: Context | None,
    ) -> None:
        if service == SERVICE_TURN_OFF:
            # Checked when sending, so lights a pending turn_on just switched
            # on are included.
            members = [
                entity_id
                for entity_id in members
                if (state := self.hass.states.get(entity_id)) is None
                or state.state != STATE_OFF
            ]
            calls = [(members, params)] if members else []
        else:
            calls = async_split_turn_on(self.hass, members, params, self._offsets)

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
                        params[ATTR_COLOR_TEMP_KELVIN],
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
