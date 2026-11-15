"""A member's lease on a fake Redis, whose ``TIME`` a test moves: what a renewal and a read answer,
when a lease lapses and to whom the lapse goes, and what a fresh namespace holds. The same scripts
run on a real Redis in ``real_redis``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from agents.mcp_server import redis_place_store
from agents.mcp_server.place_store import (
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    PlaceStoreError,
)
from agents.mcp_server.redis_place_store import CLAIM_FOR, RedisPlaceStore
from agents.member import LEASE_SECONDS
from agents.turn import Principal
from tests.support import redis_listen_support
from tests.support.mcp_server_support import a_member
from tests.support.redis_listen_support import UNUSUAL_PRINCIPALS, RedisClock, namespace_of

redis_clock = redis_listen_support.redis_clock

_PREFIX = "notes"
_ALICE = a_member("alice")
_WAIT_SECONDS = 5


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def a_pod(server: FakeServer) -> RedisPlaceStore:
    return RedisPlaceStore(FakeAsyncRedis(server=server), key_prefix=_PREFIX)


async def a_settled_namespace(pod: RedisPlaceStore, clock: RedisClock) -> None:
    """Creates the epoch and lets one lease span pass, so that a missing lease is gone."""
    await pod.live()
    clock.advance(LEASE_SECONDS)


@asynccontextmanager
async def serving(
    pod: RedisPlaceStore, *, refusals: int = 0
) -> AsyncIterator[list[tuple[Principal, str]]]:
    """Every lapse the pod's lapse service hands over, collected until the block ends; the handler
    raises on its first ``refusals`` calls."""
    handled: list[tuple[Principal, str]] = []

    async def handle(principal: Principal, generation: str) -> None:
        handled.append((principal, generation))
        if len(handled) <= refusals:
            raise RuntimeError("the host failed")

    task = asyncio.create_task(pod.serve_lapses(handle, claimer="pod"))
    try:
        yield handled
    finally:
        task.cancel()
        await asyncio.wait({task})


async def handed_over(handled: list[tuple[Principal, str]], count: int) -> None:
    async with asyncio.timeout(_WAIT_SECONDS):
        while len(handled) < count:
            await asyncio.sleep(0.01)


def test_a_renewal_extends_a_running_lease_and_starts_a_new_one_once_it_lapsed(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[LeaseRenewal, LeaseRenewal, LeaseRenewal]:
        pod = a_pod(server)
        first = await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS - 1)
        extended = await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS)
        restarted = await pod.renew(_ALICE)
        return first, extended, restarted

    first, extended, restarted = asyncio.run(scenario())

    assert (first.started, extended.started, restarted.started) == (True, False, True)
    assert extended.generation == first.generation
    assert restarted.generation != first.generation


def test_a_running_lease_holds_with_its_generation_and_after_its_lapse_holds_no_more(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[
        LeaseRenewal, LeaseStanding, LeaseRoster, LeaseStanding, list[tuple[Principal, str]]
    ]:
        pod = a_pod(server)
        await a_settled_namespace(pod, redis_clock)
        renewal = await pod.renew(_ALICE)
        running = await pod.state(_ALICE)
        roster = await pod.live()
        redis_clock.advance(LEASE_SECONDS)
        async with serving(pod) as handled:
            await handed_over(handled, 1)
            return renewal, running, roster, await pod.state(_ALICE), handled

    renewal, running, roster, lapsed, handled = asyncio.run(scenario())

    assert running == LeaseStanding(holds=True, generation=renewal.generation)
    assert roster == LeaseRoster(holders=frozenset({_ALICE}), settled=True)
    assert lapsed == LeaseStanding(holds=False, generation=None)
    assert handled == [(_ALICE, renewal.generation)]


def test_a_read_does_not_renew_the_lease(server: FakeServer, redis_clock: RedisClock) -> None:
    async def scenario() -> tuple[LeaseStanding, LeaseStanding, list[tuple[Principal, str]]]:
        pod = a_pod(server)
        await a_settled_namespace(pod, redis_clock)
        await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS - 1)
        near_the_end = await pod.state(_ALICE)
        redis_clock.advance(1)
        async with serving(pod) as handled:
            await handed_over(handled, 1)
            return near_the_end, await pod.state(_ALICE), handled

    near_the_end, at_the_end, handled = asyncio.run(scenario())

    assert near_the_end.holds is True
    assert at_the_end.holds is False
    assert len(handled) == 1


def test_a_renewal_after_a_lapse_starts_a_lease_the_read_shows_under_its_new_generation(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[str, str, LeaseStanding]:
        pod = a_pod(server)
        await a_settled_namespace(pod, redis_clock)
        old = await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS)
        new = await pod.renew(_ALICE)
        assert new.started
        return old.generation, new.generation, await pod.state(_ALICE)

    old, new, standing = asyncio.run(scenario())

    assert new != old
    assert standing == LeaseStanding(holds=True, generation=new)


def test_a_fresh_namespace_holds_every_principal_without_a_generation_for_one_lease_span(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[
        LeaseStanding, LeaseRoster, LeaseStanding, LeaseStanding, LeaseRoster
    ]:
        pod = a_pod(server)
        fresh = await pod.state(_ALICE)
        fresh_roster = await pod.live()
        redis_clock.advance(LEASE_SECONDS - 1)
        almost = await pod.state(_ALICE)
        redis_clock.advance(1)
        return fresh, fresh_roster, almost, await pod.state(_ALICE), await pod.live()

    fresh, fresh_roster, almost, settled, settled_roster = asyncio.run(scenario())

    assert fresh == LeaseStanding(holds=True, generation=None)
    assert almost == LeaseStanding(holds=True, generation=None)
    assert fresh_roster == LeaseRoster(holders=frozenset(), settled=False)
    assert settled == LeaseStanding(holds=False, generation=None)
    assert settled_roster == LeaseRoster(holders=frozenset(), settled=True)


@pytest.mark.parametrize(
    "principal",
    [*UNUSUAL_PRINCIPALS, pytest.param(a_member("a:b", iss="urn:x:y"), id="colons in both")],
)
def test_a_principal_with_unusual_text_lapses_to_its_handler_as_the_same_principal(
    server: FakeServer, redis_clock: RedisClock, principal: Principal
) -> None:
    async def scenario() -> tuple[str, frozenset[Principal], list[tuple[Principal, str]]]:
        pod = a_pod(server)
        renewal = await pod.renew(principal)
        listed = (await pod.live()).holders
        redis_clock.advance(LEASE_SECONDS)
        async with serving(a_pod(server)) as handled:
            await handed_over(handled, 1)
            return renewal.generation, listed, handled

    generation, listed, handled = asyncio.run(scenario())

    assert listed == frozenset({principal})
    assert handled == [(principal, generation)]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda pod: pod.renew(_ALICE), id="renew"),
        pytest.param(lambda pod: pod.state(_ALICE), id="state"),
        pytest.param(lambda pod: pod.live(), id="live"),
    ],
)
def test_a_failing_store_raises_place_store_error_from_each_lease_call(
    server: FakeServer, call: Callable[[RedisPlaceStore], Awaitable[object]]
) -> None:
    server.connected = False

    with pytest.raises(PlaceStoreError):
        asyncio.run(call(a_pod(server)))


def test_every_key_a_lease_writes_lies_under_the_agents_namespace(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[set[bytes], dict[str, object]]:
        pod = a_pod(server)
        await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS)
        await pod.renew(_ALICE)
        raw = FakeAsyncRedis(server=server)
        return set(await raw.keys("*")), await namespace_of(raw, _PREFIX)

    every_key, namespaced = asyncio.run(scenario())

    assert every_key
    assert {key.decode() for key in every_key} == set(namespaced)
    assert all(key.startswith(f"{_PREFIX}-agents:") for key in namespaced)


def test_a_lapse_whose_handler_raised_returns_with_the_same_generation_after_the_claim_ran_out(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[str, list[tuple[Principal, str]]]:
        pod = a_pod(server)
        await a_settled_namespace(pod, redis_clock)
        renewal = await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS)
        async with serving(a_pod(server), refusals=1) as handled:
            await handed_over(handled, 1)
            redis_clock.advance(CLAIM_FOR.total_seconds() + 1)
            await handed_over(handled, 2)
            return renewal.generation, list(handled)

    generation, handled = asyncio.run(scenario())

    assert handled == [(_ALICE, generation)] * 2


def test_a_claim_that_runs_out_under_a_running_handler_is_not_logged_with_the_generation(
    server: FakeServer,
    redis_clock: RedisClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(redis_place_store, "CLAIM_FOR", timedelta(milliseconds=300))
    caplog.set_level(logging.WARNING)

    async def scenario() -> str:
        pod = a_pod(server)
        await a_settled_namespace(pod, redis_clock)
        renewal = await pod.renew(_ALICE)
        redis_clock.advance(LEASE_SECONDS)
        running = asyncio.Event()
        release = asyncio.Event()

        async def handle(_principal: Principal, _generation: str) -> None:
            running.set()
            await release.wait()

        service = asyncio.create_task(pod.serve_lapses(handle, claimer="pod"))
        try:
            async with asyncio.timeout(_WAIT_SECONDS):
                await running.wait()
                redis_clock.advance(1)
                while not caplog.records:
                    await asyncio.sleep(0.01)
        finally:
            release.set()
            service.cancel()
            await asyncio.wait({service})
        return renewal.generation

    generation = asyncio.run(scenario())

    logged = " ".join(entry.getMessage() for entry in caplog.records)
    assert _ALICE.sub in logged
    assert generation not in logged
