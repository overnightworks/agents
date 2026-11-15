"""``RedisPlaceStore`` on a fake Redis: the address on the wire, the error mapping and the
refusals, and the listen record, whose ``TIME`` a test moves: what a store lists after another
store's listen, when a stream lapses, and what a lost reply, a lost claim or a racing stream leave
behind. The same scripts run on a real Redis in ``real_redis``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from tandem import Deadlines

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import (
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    Resubscribed,
    ToolsChange,
)
from agents.mcp_server.redis_place_store import CLAIM_FOR, RedisPlaceStore
from agents.member import LISTEN_ALLOWANCE_SECONDS, LISTEN_BOUND_SECONDS, STANDING_URI
from agents.turn import Principal
from tests.support import redis_listen_support
from tests.support.faulty_redis import EvalshaStopsAfter, LostReplyRedis, retrying
from tests.support.mcp_server_support import (
    MemberApp,
    a_member,
    listen_connection,
    open_stream,
    readable_uris,
    resolve_member,
    window_uri,
)
from tests.support.redis_listen_support import (
    UNUSUAL_PRINCIPALS,
    UNUSUAL_URIS,
    Event,
    RedisClock,
    arrived,
    delivered_up_to,
    namespace_of,
    snapshots_around_evalsha,
    subscribed,
)

redis_clock = redis_listen_support.redis_clock

_PREFIX = "notes"
_ALICE = a_member("alice")
_BOB = a_member("bob")
_ALLOWANCE = timedelta(seconds=LISTEN_ALLOWANCE_SECONDS)
_OPEN = timedelta(seconds=LISTEN_BOUND_SECONDS + LISTEN_ALLOWANCE_SECONDS)
_YES = ListeningTurn(_ALICE, STANDING_URI, listens=True)
_NO = ListeningTurn(_ALICE, STANDING_URI, listens=False)
_AFTER_EVERYTHING = ResourceChange(_BOB, STANDING_URI, "test")
_BEFORE_THE_LAPSE = ResourceChange(_BOB, STANDING_URI, "before the lapse")
_AFTER_THE_LAPSE = ResourceChange(_BOB, STANDING_URI, "after the lapse")


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def a_pod(server: FakeServer, *, key_prefix: str = _PREFIX) -> RedisPlaceStore:
    return RedisPlaceStore(FakeAsyncRedis(server=server), key_prefix=key_prefix)


@pytest.mark.parametrize(
    "signal",
    [
        ResourceChange(_ALICE, STANDING_URI, "pod-a"),
        ToolsChange(_ALICE, "pod-a"),
    ],
    ids=["a resource", "the tool list"],
)
def test_a_signal_one_pod_publishes_reaches_the_subscription_of_another(
    server: FakeServer, signal: PlaceSignal
) -> None:
    async def scenario() -> list[Event]:
        async with subscribed(a_pod(server)) as events:
            await a_pod(server).publish(signal)
            await arrived(events, 2)
            return list(events)

    assert asyncio.run(scenario()) == [Resubscribed(), signal]


def test_a_payload_that_is_no_address_is_skipped_and_logged_by_length_only(
    server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    channel = "notes-agents:tandem:v1:wake"
    foreign = [
        "not json at all",
        json.dumps({"kind": "resource"}),
        json.dumps(["v2", "resource", "iss", "sub", "uri", "pod"]),
        json.dumps(["v1", "resource", "iss", "sub", None, "pod"]),
        json.dumps(["v1", "listening-maybe", "iss", "sub", "uri", "pod"]),
    ]
    wake = ResourceChange(_ALICE, STANDING_URI, "pod-a")

    async def scenario() -> list[Event]:
        async with subscribed(a_pod(server)) as events:
            raw = FakeAsyncRedis(server=server)
            for payload in foreign:
                await raw.publish(channel, payload)
            await a_pod(server).publish(wake)
            await arrived(events, 2)
            return list(events)

    assert asyncio.run(scenario()) == [Resubscribed(), wake]
    warned = [record.getMessage() for record in caplog.records]
    assert len(warned) == len(foreign)
    assert not any("not json at all" in message for message in warned)


def test_a_failing_store_raises_place_store_error_from_publish(server: FakeServer) -> None:
    server.connected = False
    publishing = a_pod(server).publish(ToolsChange(_ALICE, "pod-a"))

    with pytest.raises(PlaceStoreError):
        asyncio.run(publishing)


def test_a_payload_that_is_no_utf8_is_skipped_and_the_subscription_goes_on(
    server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    wake = ResourceChange(_ALICE, STANDING_URI, "pod-a")
    no_utf8_payload = b"\xff\xfe\xfd"

    async def scenario() -> list[Event]:
        async with subscribed(a_pod(server)) as events:
            await FakeAsyncRedis(server=server).publish(
                "notes-agents:tandem:v1:wake", no_utf8_payload
            )
            await a_pod(server).publish(wake)
            await arrived(events, 2)
            return list(events)

    assert asyncio.run(scenario()) == [Resubscribed(), wake]
    (warning,) = caplog.records
    assert warning.levelno == logging.WARNING
    assert str(len(no_utf8_payload)) in warning.getMessage()
    assert not any(part in warning.getMessage() for part in ("xff", "xfe", "xfd", "\xff"))


@pytest.mark.parametrize(
    "key_prefix",
    [
        pytest.param("Not_A_Segment", id="no segment"),
        pytest.param("notes:more", id="two segments"),
        pytest.param("", id="empty"),
        pytest.param("notes-agents", id="ends in the namespace suffix"),
    ],
)
def test_a_key_prefix_that_is_no_segment_or_ends_in_agents_is_refused_naming_the_argument(
    server: FakeServer, key_prefix: str
) -> None:
    with pytest.raises(ValueError, match="key_prefix"):
        a_pod(server, key_prefix=key_prefix)


def test_a_change_on_one_front_wakes_a_listen_on_another(server: FakeServer) -> None:
    app = MemberApp(readable={_ALICE: readable_uris(STANDING_URI)})

    def a_front() -> tuple[McpPersonFront, ResourceChanges]:
        changes = ResourceChanges(a_pod(server))
        front = McpPersonFront([], resolve_person=resolve_member, resources=app, changes=changes)
        return front, changes

    front_a, _changes_a = a_front()
    front_b, changes_b = a_front()
    listening = listen_connection(front_a, _ALICE, [STANDING_URI])

    async def scenario() -> None:
        async with front_a.running(), front_b.running(), open_stream(listening):
            await changes_b.changed(_ALICE, STANDING_URI)
            async with asyncio.timeout(5):
                while not listening.updated_uris:
                    await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert listening.updated_uris == [STANDING_URI]


class _FakeLostReplyRedis(LostReplyRedis, FakeAsyncRedis):
    pass


def a_store_that_loses_a_reply(server: FakeServer, *, retries: int) -> RedisPlaceStore:
    client = _FakeLostReplyRedis(server=server)
    client.set_retry(retrying(retries))
    return RedisPlaceStore(client, key_prefix=_PREFIX)


class _FakeEvalshaStopsAfter(EvalshaStopsAfter, FakeAsyncRedis):
    def __init__(self, server: FakeServer, *, evalshas_that_return: int) -> None:
        FakeAsyncRedis.__init__(self, server=server)
        self._evalshas_that_return = evalshas_that_return


def a_pod_that_loses_its_connection_after(server: FakeServer, evalshas: int) -> RedisPlaceStore:
    client = _FakeEvalshaStopsAfter(server, evalshas_that_return=evalshas)
    return RedisPlaceStore(client, key_prefix=_PREFIX)


async def lapsed_at(
    clock: RedisClock, store: RedisPlaceStore, listener: Principal, seconds: float
) -> bool:
    """Whether the principal still listens ``seconds`` after the clock's start, once ``store`` ran
    its lapse."""
    clock.since_start(seconds)
    await store.lapse_due("pod")
    return not await store.listens_to(listener, STANDING_URI)


def test_a_listen_through_one_store_is_listed_by_another_and_wakes_its_subscription(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[object, ...]:
        store_a, store_b = a_pod(server), a_pod(server)
        async with subscribed(store_b) as events:
            turned = await store_a.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
            seen = await delivered_up_to(store_b, events, _AFTER_EVERYTHING)
            return (
                turned,
                seen,
                await store_b.listeners(STANDING_URI),
                await store_b.listens_to(_ALICE, STANDING_URI),
                await store_b.listens_to(_BOB, STANDING_URI),
                await store_b.listens_to(_ALICE, window_uri("first")),
            )

    turned, seen, listeners, alice_listens, bob_listens, window_listens = asyncio.run(scenario())

    assert turned == frozenset({STANDING_URI})
    assert seen == [Resubscribed(), _YES, _AFTER_EVERYTHING]
    assert listeners == frozenset({_ALICE})
    assert (alice_listens, bob_listens, window_listens) == (True, False, False)


def test_a_stream_that_honored_only_a_window_does_not_count_for_the_standing(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[frozenset[Principal], bool]:
        store = a_pod(server)
        await store.acknowledged("stream", _ALICE, [window_uri("first")], _OPEN)
        return await store.listeners(STANDING_URI), await store.listens_to(_ALICE, STANDING_URI)

    assert asyncio.run(scenario()) == (frozenset(), False)


def test_an_open_stream_that_never_ends_lapses_at_the_bound_plus_the_allowance_with_one_no(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[bool, bool, list[object]]:
        store_a, store_b = a_pod(server), a_pod(server)
        async with subscribed(store_b) as events:
            await store_a.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
            before = await lapsed_at(redis_clock, store_b, _ALICE, _OPEN.total_seconds() - 1)
            at_the_end = await lapsed_at(redis_clock, store_b, _ALICE, _OPEN.total_seconds())
            await store_a.lapse_due("pod")
            return before, at_the_end, await delivered_up_to(store_b, events, _AFTER_EVERYTHING)

    before, at_the_end, seen = asyncio.run(scenario())

    assert (before, at_the_end) == (False, True)
    assert seen == [Resubscribed(), _YES, _NO, _AFTER_EVERYTHING]


@pytest.mark.parametrize(
    ("ended_holds_for", "lapses_at"),
    [
        pytest.param(_ALLOWANCE, _ALLOWANCE, id="shortened to the allowance"),
        pytest.param(_OPEN * 2, _OPEN, id="never lengthened"),
    ],
)
def test_ended_shortens_a_streams_deadline_and_never_lengthens_it(
    server: FakeServer, redis_clock: RedisClock, ended_holds_for: timedelta, lapses_at: timedelta
) -> None:
    async def scenario() -> tuple[bool, bool]:
        store = a_pod(server)
        await store.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
        await store.ended("stream", ended_holds_for)
        seconds = lapses_at.total_seconds()
        return (
            await lapsed_at(redis_clock, store, _ALICE, seconds - 1),
            await lapsed_at(redis_clock, store, _ALICE, seconds),
        )

    assert asyncio.run(scenario()) == (False, True)


def test_ended_on_a_claimed_stream_leaves_the_claim_and_the_lapse_completes(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[list[object], bool]:
        store = a_pod(server)
        claims = Deadlines(
            FakeAsyncRedis(server=server),
            key_prefix=f"{_PREFIX}-agents",
            kind="listen",
            claim_for=CLAIM_FOR,
        )
        await store.acknowledged("stream", _ALICE, [STANDING_URI], _ALLOWANCE)
        redis_clock.advance(_ALLOWANCE.total_seconds())
        assert [due.key for due in await claims.claim_due(claimer="other", limit=1)] == ["stream"]
        await store.ended("stream", timedelta(0))
        still_claimed = await claims.claim_due(claimer="other", limit=1)
        redis_clock.advance(CLAIM_FOR.total_seconds())
        await store.lapse_due("pod")
        return still_claimed, await store.listens_to(_ALICE, STANDING_URI)

    assert asyncio.run(scenario()) == ([], False)


def test_acknowledged_twice_with_one_stream_returns_empty_publishes_nothing_and_keeps_the_deadline(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[frozenset[str], bool, bool, list[object]]:
        store_a, store_b = a_pod(server), a_pod(server)
        async with subscribed(store_b) as events:
            await store_a.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
            replayed = await store_a.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN * 2)
            before = await lapsed_at(redis_clock, store_b, _ALICE, _OPEN.total_seconds() - 1)
            at_the_end = await lapsed_at(redis_clock, store_b, _ALICE, _OPEN.total_seconds())
            return (
                replayed,
                before,
                at_the_end,
                await delivered_up_to(store_b, events, _AFTER_EVERYTHING),
            )

    replayed, before, at_the_end, seen = asyncio.run(scenario())

    assert replayed == frozenset()
    assert (before, at_the_end) == (False, True)
    assert seen == [Resubscribed(), _YES, _NO, _AFTER_EVERYTHING]


def test_a_lapse_whose_claim_was_never_finished_is_done_again_by_the_next_claim_without_a_second_no(
    server: FakeServer, redis_clock: RedisClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    async def scenario() -> list[object]:
        store_a = a_pod_that_loses_its_connection_after(server, evalshas=2)
        store_b = a_pod(server)
        async with subscribed(store_b) as events:
            await store_b.acknowledged("stream", _ALICE, [STANDING_URI], _ALLOWANCE)
            redis_clock.advance(_ALLOWANCE.total_seconds())
            await store_a.lapse_due("a")
            redis_clock.advance(CLAIM_FOR.total_seconds())
            await store_b.lapse_due("b")
            return await delivered_up_to(store_b, events, _AFTER_EVERYTHING)

    assert asyncio.run(scenario()) == [Resubscribed(), _YES, _NO, _AFTER_EVERYTHING]
    (warning,) = caplog.records
    assert warning.levelno == logging.WARNING
    assert "ConnectionError" in warning.getMessage()


def test_a_pod_that_claimed_a_stream_and_lost_its_connection_leaves_it_to_the_next_claim(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[bool, bool, list[object]]:
        store_a = a_pod_that_loses_its_connection_after(server, evalshas=1)
        store_b = a_pod(server)
        async with subscribed(store_b) as events:
            await store_b.acknowledged("stream", _ALICE, [STANDING_URI], _ALLOWANCE)
            redis_clock.advance(_ALLOWANCE.total_seconds())
            await store_a.lapse_due("a")
            await store_b.lapse_due("b")
            still_listed = await store_b.listens_to(_ALICE, STANDING_URI)
            redis_clock.advance(CLAIM_FOR.total_seconds())
            await store_b.lapse_due("b")
            return (
                still_listed,
                await store_b.listens_to(_ALICE, STANDING_URI),
                await delivered_up_to(store_b, events, _AFTER_EVERYTHING),
            )

    still_listed, listed_after, seen = asyncio.run(scenario())

    assert (still_listed, listed_after) == (True, False)
    assert seen == [Resubscribed(), _YES, _NO, _AFTER_EVERYTHING]


def test_a_lost_reply_of_acknowledged_raises_yet_the_yes_arrives_and_the_stream_lapses(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[bool, list[object]]:
        store_a = a_store_that_loses_a_reply(server, retries=0)
        store_b = a_pod(server)
        async with subscribed(store_b) as events:
            with pytest.raises(PlaceStoreError):
                await store_a.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
            await store_a.ended("stream", _ALLOWANCE)
            lapsed = await lapsed_at(redis_clock, store_b, _ALICE, LISTEN_ALLOWANCE_SECONDS)
            return lapsed, await delivered_up_to(store_b, events, _AFTER_EVERYTHING)

    lapsed, seen = asyncio.run(scenario())

    assert lapsed
    assert seen == [Resubscribed(), _YES, _NO, _AFTER_EVERYTHING]


def test_a_retry_that_replays_acknowledged_after_a_lost_reply_leaves_the_store_unchanged(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[frozenset[str], bool, list[object]]:
        store_a = a_store_that_loses_a_reply(server, retries=1)
        store_b = a_pod(server)
        async with subscribed(store_b) as events:
            replayed = await store_a.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
            at_the_end = await lapsed_at(redis_clock, store_b, _ALICE, _OPEN.total_seconds())
            return replayed, at_the_end, await delivered_up_to(store_b, events, _AFTER_EVERYTHING)

    replayed, at_the_end, seen = asyncio.run(scenario())

    assert replayed == frozenset()
    assert at_the_end
    assert seen == [Resubscribed(), _YES, _NO, _AFTER_EVERYTHING]


@pytest.mark.parametrize(
    ("new_stream_comes", "turns"),
    [
        pytest.param("before the claim", [_YES], id="before the claim"),
        pytest.param(
            "between the claim and the lapse", [_YES], id="between the claim and the lapse"
        ),
        pytest.param("after the lapse", [_YES, _NO, _YES], id="after the lapse"),
    ],
)
def test_a_new_stream_of_the_same_principal_and_uri_decides_whether_an_old_ones_lapse_turns_to_no(
    server: FakeServer, redis_clock: RedisClock, new_stream_comes: str, turns: list[ListeningTurn]
) -> None:
    async def scenario() -> tuple[bool, list[object]]:
        store_a, store_b = a_pod(server), a_pod(server)
        claims = Deadlines(
            FakeAsyncRedis(server=server),
            key_prefix=f"{_PREFIX}-agents",
            kind="listen",
            claim_for=CLAIM_FOR,
        )
        async with subscribed(store_b) as events:
            await store_a.acknowledged("old", _ALICE, [STANDING_URI], _ALLOWANCE)
            redis_clock.advance(_ALLOWANCE.total_seconds())
            if new_stream_comes == "before the claim":
                await store_a.acknowledged("new", _ALICE, [STANDING_URI], _OPEN)
                await store_b.lapse_due("b")
            elif new_stream_comes == "between the claim and the lapse":
                await claims.claim_due(claimer="other", limit=1)
                await store_a.acknowledged("new", _ALICE, [STANDING_URI], _OPEN)
                redis_clock.advance(CLAIM_FOR.total_seconds())
                await store_b.lapse_due("b")
            else:
                await store_b.lapse_due("b")
                await store_a.acknowledged("new", _ALICE, [STANDING_URI], _OPEN)
            return (
                await store_b.listens_to(_ALICE, STANDING_URI),
                await delivered_up_to(store_b, events, _AFTER_EVERYTHING),
            )

    listens, seen = asyncio.run(scenario())

    assert listens
    assert seen == [Resubscribed(), *turns, _AFTER_EVERYTHING]


def test_a_principal_turns_to_no_for_a_uri_only_when_its_last_stream_of_it_lapsed(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    window = window_uri("first")

    async def scenario() -> tuple[frozenset[str], list[object]]:
        store = a_pod(server)
        async with subscribed(a_pod(server)) as events:
            await store.acknowledged("first", _ALICE, [STANDING_URI], _ALLOWANCE)
            second = await store.acknowledged("second", _ALICE, [STANDING_URI, window], _OPEN)
            await store.acknowledged("third", _BOB, [STANDING_URI], _OPEN)
            await lapsed_at(redis_clock, store, _ALICE, LISTEN_ALLOWANCE_SECONDS)
            return second, await delivered_up_to(store, events, _AFTER_EVERYTHING)

    second, seen = asyncio.run(scenario())

    assert second == frozenset({window})
    assert seen == [
        Resubscribed(),
        _YES,
        ListeningTurn(_ALICE, window, listens=True),
        ListeningTurn(_BOB, STANDING_URI, listens=True),
        _AFTER_EVERYTHING,
    ]


@pytest.mark.parametrize("principal", UNUSUAL_PRINCIPALS)
@pytest.mark.parametrize("uri", UNUSUAL_URIS)
def test_a_lapsed_stream_leaves_no_key_and_no_member_whatever_its_principal_and_uri_hold(
    server: FakeServer, redis_clock: RedisClock, principal: Principal, uri: str
) -> None:
    async def scenario() -> tuple[
        list[object], list[str], list[object], bool, frozenset[Principal]
    ]:
        store = a_pod(server)
        raw = FakeAsyncRedis(server=server)
        async with subscribed(store) as events:
            await store.acknowledged("stream", principal, [uri], _ALLOWANCE)
            written = sorted(key.decode() for key in await raw.keys("*"))
            redis_clock.advance(_ALLOWANCE.total_seconds())
            await store.lapse_due("pod")
            return (
                await delivered_up_to(store, events, _AFTER_EVERYTHING),
                written,
                await raw.keys("*"),
                await store.listens_to(principal, uri),
                await store.listeners(uri),
            )

    seen, written, left, listens, listeners = asyncio.run(scenario())

    assert seen == [
        Resubscribed(),
        ListeningTurn(principal, uri, listens=True),
        ListeningTurn(principal, uri, listens=False),
        _AFTER_EVERYTHING,
    ]
    assert written
    assert all(key.startswith(f"{_PREFIX}-agents:") for key in written)
    assert (left, listens, listeners) == ([], False, frozenset())


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda store: store.acknowledged("s", _ALICE, [STANDING_URI], _OPEN), id="acknowledged"
        ),
        pytest.param(lambda store: store.ended("s", _ALLOWANCE), id="ended"),
        pytest.param(lambda store: store.listens_to(_ALICE, STANDING_URI), id="listens_to"),
        pytest.param(lambda store: store.listeners(STANDING_URI), id="listeners"),
        pytest.param(lambda store: store.lapse_due("pod"), id="lapse_due"),
    ],
)
def test_a_failing_store_raises_place_store_error_from_every_call_of_the_record(
    server: FakeServer, call: object
) -> None:
    server.connected = False
    store = a_pod(server)

    with pytest.raises(PlaceStoreError):
        asyncio.run(call(store))  # type: ignore[operator]


_RECORD = json.dumps([json.dumps([_ALICE.iss, _ALICE.sub]), [STANDING_URI]])
_FAR_FUTURE_MS = 10**15


def key_names(raw: FakeAsyncRedis) -> dict[str, str]:
    """The keys of the record by role: tandem's three, then the record's own two."""
    deadlines = Deadlines(raw, key_prefix=f"{_PREFIX}-agents", kind="listen", claim_for=CLAIM_FOR)
    return {
        "due": deadlines.keys.due,
        "claimed": deadlines.keys.claimed,
        "claim": deadlines.keys.claim,
        "listens": f"{_PREFIX}-agents:v1:listens",
        "streams": f"{_PREFIX}-agents:v1:streams",
    }


async def seed_a_stream_in_every_key(raw: FakeAsyncRedis, *, recorded: bool) -> None:
    """``held`` waits, is claimed and keeps a claim token at once, so a write a script makes before
    it fails shows as a change."""
    names = key_names(raw)
    await raw.zadd(names["due"], {"held": _FAR_FUTURE_MS, "other": _FAR_FUTURE_MS})
    await raw.zadd(names["claimed"], {"held": _FAR_FUTURE_MS})
    await raw.hset(names["claim"], "held", "claimer")
    await raw.zadd(names["listens"], {f"{STANDING_URI}\n[]\nother": 0})
    await raw.hset(names["streams"], "other", _RECORD)
    if recorded:
        await raw.hset(names["streams"], "held", _RECORD)


async def replace_with_a_string(raw: FakeAsyncRedis, key: str) -> None:
    await raw.delete(key)
    await raw.set(key, "not a collection")


_RECORD_KEYS = ("due", "claimed", "claim", "listens", "streams")


@pytest.mark.parametrize(
    ("call", "recorded", "wrong_key"),
    [
        pytest.param(call, recorded, key, id=f"{name} with a wrong {key}")
        for name, call, recorded, keys in [
            (
                "acknowledged",
                lambda store: store.acknowledged("held", _ALICE, [STANDING_URI], _OPEN),
                False,
                _RECORD_KEYS,
            ),
            (
                "ended",
                lambda store: store.ended("held", _ALLOWANCE),
                True,
                _RECORD_KEYS[:3] + ("streams",),
            ),
        ]
        for key in keys
    ],
)
def test_acknowledged_and_ended_refuse_a_key_of_the_wrong_type_before_they_write_anything(
    server: FakeServer,
    redis_clock: RedisClock,
    call: Callable[[RedisPlaceStore], Awaitable[object]],
    recorded: bool,
    wrong_key: str,
) -> None:
    async def scenario() -> tuple[dict[str, object], dict[str, object], list[Event]]:
        raw = FakeAsyncRedis(server=server)
        store = a_pod(server)
        await seed_a_stream_in_every_key(raw, recorded=recorded)
        await replace_with_a_string(raw, key_names(raw)[wrong_key])
        before = await namespace_of(raw, _PREFIX)
        async with subscribed(store) as events:
            with pytest.raises(PlaceStoreError):
                await call(store)
            seen = await delivered_up_to(store, events, _AFTER_EVERYTHING)
        return before, await namespace_of(raw, _PREFIX), seen

    before, after, seen = asyncio.run(scenario())

    assert after == before
    assert seen == [Resubscribed(), _AFTER_EVERYTHING]


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(
            lambda raw: replace_with_a_string(raw, key_names(raw)["listens"]), id="listens"
        ),
        pytest.param(
            lambda raw: replace_with_a_string(raw, key_names(raw)["streams"]), id="streams"
        ),
        pytest.param(lambda raw: record_held(raw, "5"), id="a record that is a number"),
        pytest.param(
            lambda raw: record_held(raw, json.dumps(["5", [STANDING_URI]])),
            id="a principal text that is a number",
        ),
    ],
)
def test_a_lapse_that_cannot_read_the_record_writes_nothing_logs_and_keeps_its_claim(
    server: FakeServer,
    redis_clock: RedisClock,
    caplog: pytest.LogCaptureFixture,
    damage: Callable[[FakeAsyncRedis], Awaitable[None]],
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    async def scenario() -> tuple[tuple[object, object], list[Event], object]:
        raw = FakeAsyncRedis(server=server)
        client = FakeAsyncRedis(server=server)
        store = RedisPlaceStore(client, key_prefix=_PREFIX)
        around_scripts = snapshots_around_evalsha(client, lambda: namespace_of(raw, _PREFIX))
        async with subscribed(store) as events:
            await store.acknowledged("held", _ALICE, [STANDING_URI], _ALLOWANCE)
            await delivered_up_to(store, events, _BEFORE_THE_LAPSE)
            await damage(raw)
            redis_clock.advance(_ALLOWANCE.total_seconds())
            await store.lapse_due("pod")
            seen = await delivered_up_to(store, events, _AFTER_THE_LAPSE)
            claimed = await raw.zrange(key_names(raw)["claimed"], 0, -1)
            return around_scripts[-1], seen, claimed

    (before, after), seen, claimed = asyncio.run(scenario())

    assert after == before
    assert seen == [Resubscribed(), _YES, _BEFORE_THE_LAPSE, _AFTER_THE_LAPSE]
    assert claimed == [b"held"]
    (warning,) = caplog.records
    assert warning.levelno == logging.WARNING


async def record_held(raw: FakeAsyncRedis, record: str) -> None:
    await raw.hset(key_names(raw)["streams"], "held", record)
