"""The place store on Redis, for an app that runs more than one pod; it needs the ``redis`` extra.

The wake channel is tandem's. Everything agents keeps in Redis lives under ``<key_prefix>-agents``,
apart from the app's own keys and channel, which sit under ``<key_prefix>``. A host imports this
module itself; ``agents.mcp_server`` does not, so an app without ``ResourceChanges`` never loads
tandem or redis.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Final, Literal

from pydantic import TypeAdapter, ValidationError
from redis.asyncio import Redis
from tandem import Resubscribed as TandemResubscribed
from tandem import StoreError, WakeChannel, checked_segment

from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import (
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    Resubscribed,
    ToolsChange,
)
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)
_NAMESPACE_SUFFIX: Final = "-agents"

type _Address = (
    tuple[Literal["v1"], Literal["resource"], str, str, str, str]
    | tuple[Literal["v1"], Literal["tools"], str, str, None, str]
)

_ADDRESS: Final[TypeAdapter[_Address]] = TypeAdapter(_Address)


def _encoded(signal: PlaceSignal) -> str:
    match signal:
        case ResourceChange(principal, uri, origin):
            return json.dumps(["v1", "resource", principal.iss, principal.sub, uri, origin])
        case ToolsChange(principal, origin):
            return json.dumps(["v1", "tools", principal.iss, principal.sub, None, origin])


def _decoded(address: str) -> PlaceSignal | None:
    """The signal an address carries, or ``None`` for a payload no ``["v1", ...]`` address."""
    try:
        match _ADDRESS.validate_json(address, strict=True):
            case ("v1", "resource", iss, sub, uri, origin):
                return ResourceChange(Principal(iss=iss, sub=sub), uri, origin)
            case ("v1", "tools", iss, sub, None, origin):
                return ToolsChange(Principal(iss=iss, sub=sub), origin)
    except ValidationError:
        return None
    return None


class RedisPlaceStore:
    """``PlaceSignals`` on tandem's wake channel under ``<key_prefix>-agents``.

    ``key_prefix`` is the app's one segment, the value it gives its own tandem. ``redis`` is the
    app's client, which carries the ``Retry`` and the socket timeouts tandem's duties name.
    """

    def __init__(self, redis: Redis, *, key_prefix: str) -> None:
        checked_segment(key_prefix, argument="key_prefix")
        if key_prefix.endswith(_NAMESPACE_SUFFIX):
            raise ValueError(f"key_prefix must not end in {_NAMESPACE_SUFFIX}")
        self._channel = WakeChannel(redis, key_prefix=key_prefix + _NAMESPACE_SUFFIX)

    async def publish(self, signal: PlaceSignal) -> None:
        try:
            await self._channel.publish(_encoded(signal))
        except StoreError as failure:
            raise PlaceStoreError(str(failure)) from failure

    async def received(self) -> AsyncIterator[PlaceSignal | Resubscribed]:
        while True:
            try:
                async for event in self._channel.received():
                    if isinstance(event, TandemResubscribed):
                        yield Resubscribed()
                        continue
                    signal = _decoded(event.address)
                    if signal is None:
                        _LOG.warning(
                            "a payload of %d characters is no address: skipped", len(event.address)
                        )
                    else:
                        yield signal
                return
            except UnicodeDecodeError as malformed:
                # Tandem decodes the payload before it yields, so the bytes end its iterator;
                # the next one subscribes again, which the front answers with a resync.
                _LOG.warning("a payload of %d bytes is no UTF-8: skipped", len(malformed.object))
            except StoreError as failure:
                raise PlaceStoreError(str(failure)) from failure
