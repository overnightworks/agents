"""The listen record on a real Redis, driven through the adapter in one process: what a fake Redis
cannot prove, the scripts on the real engine and a claim that runs out in real seconds."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from agents.mcp_server.place_store import ListeningTurn, ResourceChange, Resubscribed
from agents.mcp_server.redis_place_store import CLAIM_FOR, RedisPlaceStore
from agents.member import LISTEN_ALLOWANCE_SECONDS, LISTEN_BOUND_SECONDS, STANDING_URI
from agents.turn import Principal
from tests.support.faulty_redis import EvalshaStopsAfter
from tests.support.mcp_server_support import a_member
from tests.support.redis_listen_support import (
    UNUSUAL_PRINCIPALS,
    UNUSUAL_URIS,
    delivered_up_to,
    eventually,
    subscribed,
)

_PREFIX = "notes"
_ALICE = a_member("alice")
_OPEN = timedelta(seconds=LISTEN_BOUND_SECONDS + LISTEN_ALLOWANCE_SECONDS)
_FIRST_BARRIER = ResourceChange(a_member("bob"), STANDING_URI, "first")
_SECOND_BARRIER = ResourceChange(a_member("bob"), STANDING_URI, "second")
_SECONDS_PAST_THE_CLAIM = 10


def a_store(redis_url: str) -> RedisPlaceStore:
    return RedisPlaceStore(AsyncRedis.from_url(redis_url), key_prefix=_PREFIX)


def test_a_claim_its_pod_cannot_finish_is_lapsed_by_another_pod_once_the_claim_ran_out(
    redis_url: str, admin: Redis
) -> None:
    async def scenario() -> tuple[object, ...]:
        store_b = a_store(redis_url)
        store_a = RedisPlaceStore(
            EvalshaStopsAfter(redis_url, evalshas_that_return=1), key_prefix=_PREFIX
        )
        async with subscribed(store_b) as events:
            await store_b.acknowledged("stream", _ALICE, [STANDING_URI], _OPEN)
            await store_b.ended("stream", timedelta(0))
            await store_a.lapse_due("a")
            await store_b.lapse_due("b")
            listed_while_claimed = await store_b.listeners(STANDING_URI)
            before = await delivered_up_to(store_b, events, _FIRST_BARRIER)

            async def lapsed() -> bool:
                await store_b.lapse_due("b")
                return not await store_b.listeners(STANDING_URI)

            await eventually(lapsed, seconds=CLAIM_FOR.total_seconds() + _SECONDS_PAST_THE_CLAIM)
            return (
                listed_while_claimed,
                before,
                await delivered_up_to(store_b, events, _SECOND_BARRIER),
            )

    listed_while_claimed, before, after = asyncio.run(scenario())

    yes = ListeningTurn(_ALICE, STANDING_URI, listens=True)
    no = ListeningTurn(_ALICE, STANDING_URI, listens=False)
    assert listed_while_claimed == frozenset({_ALICE})
    assert before == [Resubscribed(), yes, _FIRST_BARRIER]
    assert after == [Resubscribed(), yes, _FIRST_BARRIER, no, _SECOND_BARRIER]
    assert admin.keys("*") == []


@pytest.mark.parametrize("principal", UNUSUAL_PRINCIPALS)
@pytest.mark.parametrize("uri", UNUSUAL_URIS)
def test_a_lapsed_stream_leaves_no_member_whatever_its_principal_and_uri_hold(
    redis_url: str, admin: Redis, principal: Principal, uri: str
) -> None:
    async def scenario() -> tuple[list[object], bool, frozenset[Principal]]:
        store = a_store(redis_url)
        async with subscribed(store) as events:
            await store.acknowledged("stream", principal, [uri], timedelta(0))
            await store.lapse_due("pod")
            return (
                await delivered_up_to(store, events, _FIRST_BARRIER),
                await store.listens_to(principal, uri),
                await store.listeners(uri),
            )

    seen, listens, listeners = asyncio.run(scenario())

    assert seen == [
        Resubscribed(),
        ListeningTurn(principal, uri, listens=True),
        ListeningTurn(principal, uri, listens=False),
        _FIRST_BARRIER,
    ]
    assert (listens, listeners) == (False, frozenset())
    assert admin.keys("*") == []
