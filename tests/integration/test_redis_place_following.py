"""``RedisPlaceStore.following``: what the entry does when the store does not confirm and what a
subscription that fails afterwards tells the host. The signals it hands over are in
``test_redis_place_store``."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fakeredis import FakeServer

from agents.mcp_server.place_store import ListeningTurn, PlaceSignal, PlaceStoreError, ToolsChange
from agents.mcp_server.redis_place_store import RedisPlaceStore
from tests.support import redis_listen_support
from tests.support.faulty_redis import RefusedAfterConfirmationRedis
from tests.support.mcp_server_support import a_member
from tests.support.redis_listen_support import (
    KEY_PREFIX,
    Event,
    Resynced,
    a_pod,
    arrived,
    subscribed,
)

server = redis_listen_support.server

_ALICE = a_member("alice")


def test_following_raises_place_store_error_when_the_subscription_is_not_confirmed_in_time(
    server: FakeServer,
) -> None:
    server.connected = False

    async def scenario() -> None:
        async with a_pod(server).following(
            on_resync=lambda: None,
            on_signal=lambda _signal: None,
            on_failed=lambda _failure: None,
            ready_within=timedelta(seconds=0.2),
        ):
            pytest.fail("the subscription was entered without a confirmation")

    entering = scenario()
    with pytest.raises(PlaceStoreError):
        asyncio.run(entering)


def test_a_subscription_that_redis_refuses_after_the_entry_calls_on_failed_once_with_the_cause(
    server: FakeServer,
) -> None:
    store = RedisPlaceStore(RefusedAfterConfirmationRedis(server=server), key_prefix=KEY_PREFIX)

    async def scenario() -> list[Event]:
        async with subscribed(store) as events:
            await arrived(events, 2)
            return list(events)

    [resynced, failure] = asyncio.run(scenario())

    assert resynced == Resynced()
    assert isinstance(failure, PlaceStoreError)
    assert type(failure.__cause__).__name__ == "StoreError"


def test_a_handler_that_raises_after_the_entry_is_called_on_failed_with_its_own_error(
    server: FakeServer,
) -> None:
    refusal = RuntimeError("the host's handler failed")
    failures: list[Event] = []

    def failing_handler(_signal: PlaceSignal | ListeningTurn) -> None:
        raise refusal

    async def scenario() -> None:
        async with a_pod(server).following(
            on_resync=lambda: None,
            on_signal=failing_handler,
            on_failed=failures.append,
            ready_within=timedelta(seconds=5),
        ):
            await a_pod(server).publish(ToolsChange(_ALICE, "pod-a"))
            await arrived(failures, 1)

    asyncio.run(scenario())

    assert failures == [refusal]
