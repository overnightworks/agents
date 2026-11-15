"""What the suites of ``RedisPlaceStore`` share: a store on a fake or real Redis, a subscription
that collects its events, and Redis ``TIME`` that a test moves."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from redis.asyncio import Redis

from agents.mcp_server.place_store import ListeningTurn, PlaceSignal, Resubscribed
from agents.mcp_server.redis_place_store import RedisPlaceStore
from agents.member import STANDING_URI
from tests.support.mcp_server_support import a_member

type Event = PlaceSignal | ListeningTurn | Resubscribed

_WAIT_SECONDS = 5

# What a member of the record holds verbatim: a principal and a URI no script may re-encode.
UNUSUAL_PRINCIPALS = [
    pytest.param(a_member("alice"), id="plain"),
    pytest.param(a_member("alice", iss="https://id.example/realms/one"), id="a slash in iss"),
    pytest.param(a_member("sub\u00e9", iss="https://id.example/\u00fc"), id="non-ASCII"),
]
UNUSUAL_URIS = [
    pytest.param(STANDING_URI, id="plain"),
    pytest.param("place://\U0001f600/standing", id="non-BMP"),
]


@dataclass
class RedisClock:
    """Seconds of Redis ``TIME`` on a fake server; ``advance`` moves it."""

    start: float = 1_000_000.0
    now: float = 1_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def since_start(self, seconds: float) -> None:
        self.now = self.start + seconds


@pytest.fixture
def redis_clock(monkeypatch: pytest.MonkeyPatch) -> RedisClock:
    clock = RedisClock()
    monkeypatch.setattr(time, "time", lambda: clock.now)
    return clock


@asynccontextmanager
async def subscribed(store: RedisPlaceStore) -> AsyncIterator[list[Event]]:
    """The events one subscription yields, collected until the block ends; the block starts once
    the subscription is confirmed."""
    events: list[Event] = []
    first_event_or_end = asyncio.Event()

    async def collect() -> None:
        try:
            async for event in store.received():
                events.append(event)
                first_event_or_end.set()
        finally:
            first_event_or_end.set()

    task = asyncio.create_task(collect())
    try:
        async with asyncio.timeout(_WAIT_SECONDS):
            await first_event_or_end.wait()
        if task.done():
            await task
        yield events
    finally:
        task.cancel()
        await asyncio.wait({task})


async def arrived(events: list[Event], count: int) -> None:
    async with asyncio.timeout(_WAIT_SECONDS):
        while len(events) < count:
            await asyncio.sleep(0.01)


async def delivered_up_to(
    store: RedisPlaceStore, events: list[Event], signal: PlaceSignal
) -> list[Event]:
    """The events once ``signal``, which the store publishes after everything so far, has come
    through the subscription: what was published before it has arrived too."""
    await store.publish(signal)
    async with asyncio.timeout(_WAIT_SECONDS):
        while signal not in events:
            await asyncio.sleep(0.01)
    return list(events)


async def eventually(check: Callable[[], Awaitable[bool]], *, seconds: float) -> None:
    async with asyncio.timeout(seconds):
        while not await check():
            await asyncio.sleep(0.1)


async def namespace_of(raw: Redis, prefix: str) -> dict[str, object]:
    """Every key of the app that owns ``prefix`` with its type and what it holds, scores and
    claim tokens included."""
    state: dict[str, object] = {}
    for key in sorted(await raw.keys(f"{prefix}-agents:*")):
        match (await raw.type(key)).decode():
            case "hash":
                state[key.decode()] = await raw.hgetall(key)
            case "zset":
                state[key.decode()] = await raw.zrange(key, 0, -1, withscores=True)
            case "string":
                state[key.decode()] = await raw.get(key)
            case other:
                state[key.decode()] = other
    return state


def snapshots_around_evalsha[Snapshot](
    client: Redis, snapshot: Callable[[], Awaitable[Snapshot]]
) -> list[tuple[Snapshot, Snapshot]]:
    """Take ``snapshot`` before and after every ``evalsha`` of ``client``, a script that raises
    included, and collect the pairs in call order; the last pair is the script that ran last."""
    pairs: list[tuple[Snapshot, Snapshot]] = []
    evalsha = client.evalsha

    async def watched(*arguments: Any, **options: Any) -> Any:
        before = await snapshot()
        try:
            return await evalsha(*arguments, **options)
        finally:
            pairs.append((before, await snapshot()))

    client.evalsha = watched  # type: ignore[method-assign]
    return pairs
