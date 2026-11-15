"""What the suites of ``RedisPlaceStore`` share: a store on a fake or real Redis, a subscription
that collects its events, and Redis ``TIME`` that a test moves."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from agents.mcp_server.place_store import ListeningTurn, PlaceSignal, Resubscribed
from agents.mcp_server.redis_place_store import RedisPlaceStore

type Event = PlaceSignal | ListeningTurn | Resubscribed

_WAIT_SECONDS = 5


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
