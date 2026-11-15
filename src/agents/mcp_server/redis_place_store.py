"""The place store on Redis, for an app that runs more than one pod; it needs the ``redis`` extra.

The wake channel and the deadlines are tandem's. Everything agents keeps in Redis lives under
``<key_prefix>-agents``, apart from the app's own keys and channel, which sit under
``<key_prefix>``. A host imports this module itself; ``agents.mcp_server`` does not, so an app
without ``ResourceChanges`` never loads tandem.

The listen record is a deadline of kind ``listen`` per stream, keyed by the stream's id, beside
two keys: ``v1:listens``, a lexical sorted set whose members join the URI, the principal text and
the stream by newlines, and ``v1:streams``, a hash from the stream to its record. Redis does not
roll a script back, so each script reads and validates everything before its first write.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Collection, Generator
from contextlib import contextmanager
from datetime import timedelta
from typing import Final, Literal, cast
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from tandem import Client, Deadlines, Script, StoreError, WakeChannel, checked_segment, wake_channel
from tandem import Resubscribed as TandemResubscribed

from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import (
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    Resubscribed,
    ToolsChange,
)
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)
_NAMESPACE_SUFFIX: Final = "-agents"
CLAIM_FOR: Final = timedelta(seconds=5)
_LAPSE_BATCH: Final = 20
_MILLISECOND: Final = timedelta(milliseconds=1)

# Every script takes the same KEYS: tandem's due, claimed and claim, then the listens and streams.
# A member of listens joins the URI, the principal text and the stream by newlines; the principal
# text holds no raw newline and Lua never re-encodes it.
_PRELUDE: Final = r"""
local keys = {due = KEYS[1], claimed = KEYS[2], claim = KEYS[3]}
local listens = KEYS[4]
local streams = KEYS[5]

-- Redis does not roll a script back and tandem.deadline_set writes key by key, so a key of the
-- wrong type is refused before the first write.
local function expect_type(key, expected)
  local actual = redis.call('TYPE', key)['ok']
  if actual ~= 'none' and actual ~= expected then
    error(key .. ' holds a ' .. actual .. ', not a ' .. expected)
  end
end

local function member(uri, principal_text, stream)
  return uri .. '\n' .. principal_text .. '\n' .. stream
end

local function held_by_another(uri, principal_text, stream)
  local prefix = uri .. '\n' .. principal_text .. '\n'
  local members = redis.call('ZRANGEBYLEX', listens, '[' .. prefix, '[' .. prefix .. '\255',
    'LIMIT', 0, 2)
  for _, held in ipairs(members) do
    if held ~= member(uri, principal_text, stream) then
      return true
    end
  end
  return false
end

local function turn(direction, principal, uri, origin)
  return cjson.encode({'v1', direction, principal[1], principal[2], uri, origin})
end
"""

# ARGV: stream, principal text, due in ms, origin, wake channel, record, then the URIs. A stream
# whose record exists is a replay and writes nothing; deadline_set is the first write.
_ACKNOWLEDGED: Final = (
    _PRELUDE
    + r"""
expect_type(keys.due, 'zset')
expect_type(keys.claimed, 'zset')
expect_type(keys.claim, 'hash')
expect_type(listens, 'zset')
expect_type(streams, 'hash')
local stream = ARGV[1]
if redis.call('HEXISTS', streams, stream) == 1 then
  return {}
end
local due_in_ms = tonumber(ARGV[3])
if not due_in_ms then
  error('the due time is no number')
end
local principal = cjson.decode(ARGV[2])
local turned = {}
for index = 7, #ARGV do
  if not held_by_another(ARGV[index], ARGV[2], stream) then
    turned[#turned + 1] = ARGV[index]
  end
end
tandem.deadline_set(keys, stream, due_in_ms)
redis.call('HSET', streams, stream, ARGV[6])
for index = 7, #ARGV do
  redis.call('ZADD', listens, 0, member(ARGV[index], ARGV[2], stream))
end
for _, uri in ipairs(turned) do
  tandem.wake(ARGV[5], turn('listening-yes', principal, uri, ARGV[4]))
end
return turned
"""
)

# ARGV: stream, the allowance in ms. Only a recorded stream that still waits longer is shortened.
_ENDED: Final = (
    _PRELUDE
    + r"""
local due_in_ms = tonumber(ARGV[2])
if not due_in_ms then
  error('the due time is no number')
end
expect_type(keys.due, 'zset')
expect_type(keys.claimed, 'zset')
expect_type(keys.claim, 'hash')
expect_type(streams, 'hash')
local state = tandem.deadline_state(keys, ARGV[1])
if redis.call('HEXISTS', streams, ARGV[1]) == 0 or not state or state[1] ~= 'waiting'
    or state[2] <= tandem.now_ms() + due_in_ms then
  return 0
end
tandem.deadline_set(keys, ARGV[1], due_in_ms)
return 1
"""
)

# ARGV: stream, origin, wake channel. Only a claimed stream with a record lapses; the record is
# decoded before the first write, and a principal's last stream of a URI publishes its no.
_LAPSED: Final = (
    _PRELUDE
    + r"""
local stream = ARGV[1]
expect_type(keys.due, 'zset')
expect_type(keys.claimed, 'zset')
expect_type(listens, 'zset')
expect_type(streams, 'hash')
local state = tandem.deadline_state(keys, stream)
local record = redis.call('HGET', streams, stream)
if not state or state[1] ~= 'claimed' or not record then
  return 0
end
local decoded = cjson.decode(record)
local principal_text, uris = decoded[1], decoded[2]
local principal = cjson.decode(principal_text)
if type(principal) ~= 'table' then
  error('the record of ' .. stream .. ' holds no principal')
end
local turned = {}
for _, uri in ipairs(uris) do
  if not held_by_another(uri, principal_text, stream) then
    turned[#turned + 1] = uri
  end
end
redis.call('HDEL', streams, stream)
for _, uri in ipairs(uris) do
  redis.call('ZREM', listens, member(uri, principal_text, stream))
end
for _, uri in ipairs(turned) do
  tandem.wake(ARGV[3], turn('listening-no', principal, uri, ARGV[2]))
end
return #turned
"""
)

# ARGV: a member prefix and the most to return (-1 for all): the members that start with it.
_MEMBERS: Final = r"""
return redis.call('ZRANGEBYLEX', KEYS[4], '[' .. ARGV[1], '[' .. ARGV[1] .. '\255',
  'LIMIT', 0, tonumber(ARGV[2]))
"""

type _Address = (
    tuple[Literal["v1"], Literal["resource"], str, str, str, str]
    | tuple[Literal["v1"], Literal["tools"], str, str, None, str]
    | tuple[Literal["v1"], Literal["listening-yes", "listening-no"], str, str, str, str]
)

_ADDRESS: Final[TypeAdapter[_Address]] = TypeAdapter(_Address)


def _encoded(signal: PlaceSignal) -> str:
    match signal:
        case ResourceChange(principal, uri, origin):
            return json.dumps(["v1", "resource", principal.iss, principal.sub, uri, origin])
        case ToolsChange(principal, origin):
            return json.dumps(["v1", "tools", principal.iss, principal.sub, None, origin])


def _decoded(address: str) -> PlaceSignal | ListeningTurn | None:
    """The signal or turn an address carries, or ``None`` for a payload no ``["v1", ...]``
    address; a turn's origin is dropped."""
    try:
        match _ADDRESS.validate_json(address, strict=True):
            case ("v1", "resource", iss, sub, uri, origin):
                return ResourceChange(Principal(iss=iss, sub=sub), uri, origin)
            case ("v1", "tools", iss, sub, None, origin):
                return ToolsChange(Principal(iss=iss, sub=sub), origin)
            case ("v1", ("listening-yes" | "listening-no") as direction, iss, sub, uri, _):
                return ListeningTurn(
                    Principal(iss=iss, sub=sub), uri, listens=direction == "listening-yes"
                )
    except ValidationError:
        return None
    return None


def _principal_text(principal: Principal) -> str:
    """The text a member carries for the principal; Lua never re-encodes it."""
    return json.dumps([principal.iss, principal.sub])


def _principal_from(text: str) -> Principal:
    iss, sub = json.loads(text)
    return Principal(iss=iss, sub=sub)


def _milliseconds(span: timedelta) -> int:
    return span // _MILLISECOND


@contextmanager
def _store_errors() -> Generator[None]:
    try:
        yield
    except StoreError as failure:
        raise PlaceStoreError(str(failure)) from failure


class RedisPlaceStore:
    """``PlaceStore`` on tandem's wake channel and deadlines under ``<key_prefix>-agents``.

    ``key_prefix`` is the app's one segment, the value it gives its own tandem. ``redis`` is the
    app's client, which carries the ``Retry`` and the socket timeouts tandem's duties name. A
    stream's deadline and the answers a pod reads are on Redis ``TIME``.
    """

    def __init__(self, redis: Client, *, key_prefix: str) -> None:
        checked_segment(key_prefix, argument="key_prefix")
        if key_prefix.endswith(_NAMESPACE_SUFFIX):
            raise ValueError(f"key_prefix must not end in {_NAMESPACE_SUFFIX}")
        namespace = key_prefix + _NAMESPACE_SUFFIX
        self._redis = redis
        self._origin = uuid4().hex
        self._channel = WakeChannel(redis, key_prefix=namespace)
        self._wake_channel = wake_channel(namespace)
        self._deadlines = Deadlines(redis, key_prefix=namespace, kind="listen", claim_for=CLAIM_FOR)
        deadline_keys = self._deadlines.keys
        self._keys = [
            deadline_keys.due,
            deadline_keys.claimed,
            deadline_keys.claim,
            f"{namespace}:v1:listens",
            f"{namespace}:v1:streams",
        ]
        self._acknowledged = Script(redis, _ACKNOWLEDGED)
        self._ended = Script(redis, _ENDED)
        self._lapse = Script(redis, _LAPSED)
        self._members = Script(redis, _MEMBERS)

    async def publish(self, signal: PlaceSignal) -> None:
        with _store_errors():
            await self._channel.publish(_encoded(signal))

    async def received(self) -> AsyncIterator[PlaceSignal | ListeningTurn | Resubscribed]:
        with _store_errors():
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

    async def acknowledged(
        self, stream: str, principal: Principal, uris: Collection[str], holds_for: timedelta
    ) -> frozenset[str]:
        text = _principal_text(principal)
        distinct = list(dict.fromkeys(uris))
        record = json.dumps([text, distinct], ensure_ascii=False)
        arguments: list[str | int | bytes] = [
            stream,
            text,
            _milliseconds(holds_for),
            self._origin,
            self._wake_channel,
            record,
            *distinct,
        ]
        with _store_errors():
            turned = await self._acknowledged.run(self._keys, arguments)
        return frozenset(self._texts(turned))

    async def ended(self, stream: str, holds_for: timedelta) -> None:
        with _store_errors():
            await self._ended.run(self._keys, [stream, _milliseconds(holds_for)])

    async def listens_to(self, principal: Principal, uri: str) -> bool:
        return bool(await self._members_of(f"{uri}\n{_principal_text(principal)}\n", 1))

    async def listeners(self, uri: str) -> frozenset[Principal]:
        texts = {member.split("\n")[1] for member in await self._members_of(f"{uri}\n", -1)}
        return frozenset(_principal_from(text) for text in texts)

    async def lapse_due(self, claimer: str) -> None:
        with _store_errors():
            claims = await self._deadlines.claim_due(claimer=claimer, limit=_LAPSE_BATCH)
        for due in claims:
            try:
                await self._lapse.run(self._keys, [due.key, self._origin, self._wake_channel])
                await self._deadlines.done(due)
            except StoreError as failure:
                _LOG.warning(
                    "a listen's lapse failed with %s and its claim expires",
                    type(failure.__cause__).__name__,
                )

    async def _members_of(self, prefix: str, limit: int) -> list[str]:
        with _store_errors():
            return self._texts(await self._members.run(self._keys, [prefix, limit]))

    def _texts(self, reply: object) -> list[str]:
        encoder = self._redis.get_encoder()
        return [encoder.decode(item, force=True) for item in cast(list[object], reply)]
