"""``RedisPlaceStore`` on a fake Redis when the listen record is damaged: a call that finds a key of
the wrong type refuses before it writes anything, and a lapse that cannot read a stream's record
writes nothing and keeps its claim. The same scripts run on a real Redis in ``tests/e2e``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from agents.mcp_server.place_store import ListeningTurn, PlaceStoreError, ResourceChange
from agents.mcp_server.redis_place_store import RedisPlaceStore
from agents.member import LISTEN_ALLOWANCE_SECONDS, LISTEN_BOUND_SECONDS, STANDING_URI
from tests.support import redis_listen_support
from tests.support.log_lines import LogLines, log_lines
from tests.support.mcp_server_support import a_member
from tests.support.place_store_support import lapsing
from tests.support.redis_listen_support import (
    Event,
    RedisClock,
    Resynced,
    a_pod,
    delivered_up_to,
    eventually,
    handling_failed,
    key_names,
    namespace_of,
    snapshots_around_evalsha,
    subscribed,
)

pytestmark = pytest.mark.integration

redis_clock = redis_listen_support.redis_clock
server = redis_listen_support.server
log_lines = log_lines

_PREFIX = "notes"
_ALICE = a_member("alice")
_BOB = a_member("bob")
_ALLOWANCE = timedelta(seconds=LISTEN_ALLOWANCE_SECONDS)
_OPEN = timedelta(seconds=LISTEN_BOUND_SECONDS + LISTEN_ALLOWANCE_SECONDS)
_YES = ListeningTurn(_ALICE, STANDING_URI, listens=True)
_EVERY = timedelta(milliseconds=50)
_WAIT_SECONDS = 10
_AFTER_EVERYTHING = ResourceChange(_BOB, STANDING_URI, "test")
_BEFORE_THE_LAPSE = ResourceChange(_BOB, STANDING_URI, "before the lapse")
_AFTER_THE_LAPSE = ResourceChange(_BOB, STANDING_URI, "after the lapse")


_RECORD = json.dumps([json.dumps([_ALICE.iss, _ALICE.sub]), [STANDING_URI]])
_FAR_FUTURE_MS = 10**15


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
    assert seen == [Resynced(), _AFTER_EVERYTHING]


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
def test_a_lapse_that_cannot_read_the_record_writes_nothing_and_keeps_its_claim(
    server: FakeServer,
    redis_clock: RedisClock,
    log_lines: LogLines,
    damage: Callable[[FakeAsyncRedis], Awaitable[None]],
) -> None:
    async def scenario() -> tuple[list[tuple[object, object]], list[Event], object, list[object]]:
        raw = FakeAsyncRedis(server=server)
        client = FakeAsyncRedis(server=server)
        store = RedisPlaceStore(client, key_prefix=_PREFIX)
        around_scripts = snapshots_around_evalsha(client, lambda: namespace_of(raw, _PREFIX))
        async with subscribed(store) as events:
            await store.acknowledged("held", _ALICE, [STANDING_URI], _ALLOWANCE)
            await delivered_up_to(store, events, _BEFORE_THE_LAPSE)
            await damage(raw)
            redis_clock.advance(_ALLOWANCE.total_seconds())
            scripts_before = len(around_scripts)
            async with lapsing(store, every=_EVERY) as turns:
                await eventually(lambda: handling_failed(log_lines), seconds=_WAIT_SECONDS)
            scripts_run = around_scripts[scripts_before:]
            seen = await delivered_up_to(store, events, _AFTER_THE_LAPSE)
            claimed_keys = await raw.zrange(key_names(raw)["claimed"], 0, -1)
            return scripts_run, seen, claimed_keys, list(turns)

    scripts_run, seen, claimed_keys, turns = asyncio.run(scenario())

    assert len(scripts_run) > 1
    assert len([run for run in scripts_run if run[0] != run[1]]) == 1
    assert seen == [Resynced(), _YES, _BEFORE_THE_LAPSE, _AFTER_THE_LAPSE]
    assert claimed_keys == [b"held"]
    assert turns == []


async def record_held(raw: FakeAsyncRedis, record: str) -> None:
    await raw.hset(key_names(raw)["streams"], "held", record)
