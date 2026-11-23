"""The place store on Redis, for an app that runs more than one pod; it needs the ``redis`` extra.

The wake channel and the deadlines are tandem's. Everything agents keeps in Redis lives under
``<key_prefix>-agents``, apart from the app's own keys and channel, which sit under
``<key_prefix>``. A host imports this module itself; ``agents.mcp_server`` does not, so an app
without ``ResourceChanges`` never loads tandem.

The listen record is a deadline of kind ``listen`` per stream, keyed by the stream's id, beside
two keys: ``v1:listens``, a lexical sorted set whose members join the URI, the principal text and
the stream by newlines, and ``v1:streams``, a hash from the stream to its record. Redis does not
roll a script back, so each script reads and validates everything before its first write.

A member's lease is tandem's ``Leases`` of family ``member`` and lasts ``LEASE_SECONDS``; its keys
are tandem's, ``<key_prefix>-agents:tandem:v1:epoch`` and
``<key_prefix>-agents:tandem:v1:l:member:*``, and the holder is the principal text. A principal
whose lease the store may have lost reads as holding, with no generation, for ``LEASE_SECONDS``
after the epoch began. Beside each lease a member's facts, ``MemberFacts``, are one string at
``<key_prefix>-agents:v1:member-facts:<principal text>`` that expires on Redis ``TIME`` after
``LEASE_SECONDS`` from the renewal that wrote it; it is written after the lease step and read
for a batch of principals in one script that raises on a key holding no string.
"""

from __future__ import annotations

import json
import logging
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Collection,
    Generator,
    Mapping,
)
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Final, Literal, NoReturn, cast
from uuid import uuid4

from logline import current_traceparent
from pydantic import TypeAdapter, ValidationError
from tandem import (
    Client,
    Deadlines,
    Due,
    Lapse,
    Leases,
    Liveness,
    Script,
    StoreError,
    Wake,
    WakeChannel,
    checked_segment,
    wake_channel,
)

from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import (
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    ToolsChange,
)
from agents.member import LEASE_SECONDS, MemberFacts
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)
_NAMESPACE_SUFFIX: Final = "-agents"
CLAIM_FOR: Final = timedelta(seconds=5)
_LEASE_FAMILY: Final = "member"
_LEASE_LASTS: Final = timedelta(seconds=LEASE_SECONDS)
_FACTS_KEY_INFIX: Final = ":v1:member-facts:"
_FACTS_UNREADABLE: Final = "a stored member's facts are unreadable"
_LEASE_SWEEP_EVERY: Final = timedelta(seconds=1)
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

# ARGV: stream, principal text, due in ms, origin, wake channel, record, the traceparent of the
# caller (empty outside a trace), then the URIs. A stream whose record exists is a replay and
# writes nothing; deadline_set is the first write.
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
for index = 8, #ARGV do
  if not held_by_another(ARGV[index], ARGV[2], stream) then
    turned[#turned + 1] = ARGV[index]
  end
end
tandem.deadline_set(keys, stream, due_in_ms)
redis.call('HSET', streams, stream, ARGV[6])
for index = 8, #ARGV do
  redis.call('ZADD', listens, 0, member(ARGV[index], ARGV[2], stream))
end
for _, uri in ipairs(turned) do
  tandem.wake(ARGV[5], turn('listening-yes', principal, uri, ARGV[4]), ARGV[7])
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

# ARGV: stream, origin, wake channel, the traceparent of the caller (empty outside a trace). Only a
# claimed stream with a record lapses; the record is decoded before the first write, and a
# principal's last stream of a URI publishes its no. The reply is the principal text and the URIs
# turned to no, or empty when none turned.
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
  return {}
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
  tandem.wake(ARGV[3], turn('listening-no', principal, uri, ARGV[2]), ARGV[4])
end
if #turned == 0 then
  return {}
end
return {principal_text, unpack(turned)}
"""
)

# ARGV: a member prefix and the most to return (-1 for all): the members that start with it.
_MEMBERS: Final = r"""
return redis.call('ZRANGEBYLEX', KEYS[4], '[' .. ARGV[1], '[' .. ARGV[1] .. '\255',
  'LIMIT', 0, tonumber(ARGV[2]))
"""

# KEYS: the facts key. ARGV: the facts as JSON and the span in ms. SET replaces a key of any type
# and the key expires on the server's clock.
_FACTS_KEPT: Final = r"""
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return 1
"""

# KEYS: the facts keys of a batch; the reply holds each one's string, or false when it is absent.
# A key holding no string raises before any reply, and the error names the key, never a value.
_FACTS_READ: Final = r"""
local facts = {}
for index, key in ipairs(KEYS) do
  local actual = redis.call('TYPE', key)['ok']
  if actual == 'none' then
    facts[index] = false
  elseif actual == 'string' then
    facts[index] = redis.call('GET', key)
  else
    error(key .. ' holds a ' .. actual .. ', not a string')
  end
end
return facts
"""

type _Address = (
    tuple[Literal["v1"], Literal["resource"], str, str, str, str]
    | tuple[Literal["v1"], Literal["tools"], str, str, None, str]
    | tuple[Literal["v1"], Literal["listening-yes", "listening-no"], str, str, str, str]
)

_ADDRESS: Final[TypeAdapter[_Address]] = TypeAdapter(_Address)
_FACTS: Final[TypeAdapter[MemberFacts]] = TypeAdapter(MemberFacts)


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


def _place_store_error(failure: StoreError) -> PlaceStoreError:
    refusal = PlaceStoreError(str(failure))
    refusal.__cause__ = failure
    return refusal


@contextmanager
def _store_errors() -> Generator[None]:
    try:
        yield
    except StoreError as failure:
        raise _place_store_error(failure) from failure


class RedisPlaceStore:
    """``PlaceStore`` and ``LeaseRecord`` on tandem's wake channel, deadlines and leases under
    ``<key_prefix>-agents``.

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
        self._leases = Leases(
            redis,
            key_prefix=namespace,
            family=_LEASE_FAMILY,
            lasts=_LEASE_LASTS,
            claim_for=CLAIM_FOR,
        )
        self._facts_prefix = namespace + _FACTS_KEY_INFIX
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
        self._facts_kept = Script(redis, _FACTS_KEPT)
        self._facts_read = Script(redis, _FACTS_READ)

    async def publish(self, signal: PlaceSignal) -> None:
        with _store_errors():
            await self._channel.publish(_encoded(signal))

    @asynccontextmanager
    async def following(
        self,
        *,
        on_resync: Callable[[], None],
        on_signal: Callable[[PlaceSignal | ListeningTurn], None],
        on_failed: Callable[[Exception], None],
        ready_within: timedelta,
    ) -> AsyncGenerator[None]:
        def on_wake(wake: Wake) -> None:
            signal = _decoded(wake.address)
            if signal is None:
                _LOG.info(
                    "place_signal.refused",
                    extra={"reason": "not_an_address", "character_count": len(wake.address)},
                )
            else:
                on_signal(signal)

        def on_wake_failed(failure: Exception) -> None:
            on_failed(_place_store_error(failure) if isinstance(failure, StoreError) else failure)

        async with AsyncExitStack() as entered:
            with _store_errors():
                await entered.enter_async_context(
                    self._channel.following(
                        on_resync=on_resync,
                        on_wake=on_wake,
                        on_failed=on_wake_failed,
                        ready_within=ready_within,
                    )
                )
            yield

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
            current_traceparent() or "",
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

    async def serve_stream_lapses(
        self, lapsed: Callable[[ListeningTurn], None], *, every: timedelta
    ) -> NoReturn:
        async def handle(due: Due) -> None:
            for turn in await self._lapsed_turns(due.key):
                lapsed(turn)

        await self._deadlines.serve_due(handle, every=every)

    async def renew(self, principal: Principal, facts: MemberFacts) -> LeaseRenewal:
        text = _principal_text(principal)
        with _store_errors():
            renewal = await self._leases.renew(text)
            await self._facts_kept.run(
                [self._facts_prefix + text],
                [facts.model_dump_json(), _milliseconds(_LEASE_LASTS)],
            )
        return LeaseRenewal(started=renewal.started, generation=renewal.generation)

    async def facts(self, principals: Collection[Principal]) -> Mapping[Principal, MemberFacts]:
        batch = list(dict.fromkeys(principals))
        if not batch:
            return {}
        keys = [self._facts_prefix + _principal_text(principal) for principal in batch]
        unreadable = False
        replies: list[bytes | str | None] = []
        try:
            with _store_errors():
                replies = cast(list[bytes | str | None], await self._facts_read.run(keys, []))
        except UnicodeDecodeError:
            # A decoding client fails on the stored bytes; its error holds the whole value.
            unreadable = True
        if unreadable:
            raise PlaceStoreError(_FACTS_UNREADABLE)
        found: dict[Principal, MemberFacts] = {}
        for principal, stored in zip(batch, replies, strict=True):
            if stored is None:
                continue
            try:
                found[principal] = _FACTS.validate_json(stored)
            except ValidationError:
                unreadable = True
        # Raised outside the except block, so no owner or name rides the error's context.
        if unreadable:
            raise PlaceStoreError(_FACTS_UNREADABLE)
        return found

    async def state(self, principal: Principal) -> LeaseStanding:
        holder = _principal_text(principal)
        with _store_errors():
            standing = (await self._leases.state([holder]))[holder]
        return LeaseStanding(
            holds=standing.liveness is not Liveness.GONE, generation=standing.generation
        )

    async def live(self) -> LeaseRoster:
        with _store_errors():
            roster = await self._leases.live()
        return LeaseRoster(
            holders=frozenset(_principal_from(holder) for holder in roster.holders),
            settled=roster.settled,
        )

    async def serve_lapses(self, handle: Callable[[Principal, str], Awaitable[None]]) -> NoReturn:
        async def hand_over(lapse: Lapse) -> None:
            await handle(_principal_from(lapse.holder), lapse.generation)

        await self._leases.serve_lapses(hand_over, every=_LEASE_SWEEP_EVERY)

    async def _lapsed_turns(self, stream: str) -> list[ListeningTurn]:
        reply = await self._lapse.run(
            self._keys,
            [stream, self._origin, self._wake_channel, current_traceparent() or ""],
        )
        texts = self._texts(reply)
        if not texts:
            return []
        principal = _principal_from(texts[0])
        return [ListeningTurn(principal, uri, listens=False) for uri in texts[1:]]

    async def _members_of(self, prefix: str, limit: int) -> list[str]:
        with _store_errors():
            return self._texts(await self._members.run(self._keys, [prefix, limit]))

    def _texts(self, reply: object) -> list[str]:
        encoder = self._redis.get_encoder()
        return [encoder.decode(item, force=True) for item in cast(list[object], reply)]
