"""A member's lease on a fake Redis, whose ``TIME`` a test moves: what a renewal and a read answer,
when a lease lapses and to whom the lapse goes, and what a fresh namespace holds. The same scripts
run on a real Redis in ``real_redis``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from agents.mcp_server import MemberLeases
from agents.mcp_server.place_store import (
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    PlaceStoreError,
)
from agents.mcp_server.redis_place_store import CLAIM_FOR, RedisPlaceStore
from agents.member import LEASE_SECONDS, MemberFacts
from agents.turn import Principal
from tests.support import redis_listen_support
from tests.support.faulty_redis import FakeEvalshaStopsAfter
from tests.support.log_lines import LogLines, log_lines
from tests.support.mcp_server_support import a_member, a_member_facts
from tests.support.redis_listen_support import UNUSUAL_PRINCIPALS, RedisClock, namespace_of

redis_clock = redis_listen_support.redis_clock
log_lines = log_lines

_PREFIX = "notes"
_ALICE = a_member("alice")
_FACTS = a_member_facts()
_WAIT_SECONDS = 5


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def a_pod(server: FakeServer, *, decoding: bool = False) -> RedisPlaceStore:
    client = FakeAsyncRedis(server=server, decode_responses=decoding)
    return RedisPlaceStore(client, key_prefix=_PREFIX)


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

    task = asyncio.create_task(pod.serve_lapses(handle))
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
        first = await pod.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS - 1)
        extended = await pod.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS)
        restarted = await pod.renew(_ALICE, _FACTS)
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
        renewal = await pod.renew(_ALICE, _FACTS)
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
        await pod.renew(_ALICE, _FACTS)
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
        old = await pod.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS)
        new = await pod.renew(_ALICE, _FACTS)
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
        renewal = await pod.renew(principal, _FACTS)
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
        pytest.param(lambda pod: pod.renew(_ALICE, _FACTS), id="renew"),
        pytest.param(lambda pod: pod.facts([_ALICE]), id="facts"),
        pytest.param(lambda pod: pod.state(_ALICE), id="state"),
        pytest.param(lambda pod: pod.live(), id="live"),
    ],
)
def test_a_failing_store_raises_place_store_error_from_each_lease_call(
    server: FakeServer, call: Callable[[RedisPlaceStore], Awaitable[object]]
) -> None:
    server.connected = False
    lease_call = call(a_pod(server))

    with pytest.raises(PlaceStoreError):
        asyncio.run(lease_call)


def test_every_key_a_lease_writes_lies_under_the_agents_namespace(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[set[bytes], dict[str, object]]:
        pod = a_pod(server)
        await pod.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS)
        await pod.renew(_ALICE, _FACTS)
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
        renewal = await pod.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS)
        async with serving(a_pod(server), refusals=1) as handled:
            await handed_over(handled, 1)
            redis_clock.advance(CLAIM_FOR.total_seconds() + 1)
            await handed_over(handled, 2)
            return renewal.generation, list(handled)

    generation, handled = asyncio.run(scenario())

    assert handled == [(_ALICE, generation)] * 2


def test_leaving_running_during_a_handler_cuts_it_and_the_lapse_is_handed_over_again(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[str, list[bool], int, list[tuple[Principal, str]]]:
        pod = a_pod(server)
        await a_settled_namespace(pod, redis_clock)
        started = asyncio.Event()
        cut: list[bool] = []

        async def handle(_principal: Principal, _generation: str) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cut.append(True)
                raise

        leases = MemberLeases(pod, lease_ended=handle)
        renewal = await leases.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS)

        async def run_until_cancelled() -> None:
            async with leases.running():
                await asyncio.Event().wait()

        runner = asyncio.create_task(run_until_cancelled())
        async with asyncio.timeout(_WAIT_SECONDS):
            await started.wait()
        runner.cancel()
        await asyncio.wait({runner})
        assert runner.cancelled()
        left = len(asyncio.all_tasks()) - 1
        redis_clock.advance(CLAIM_FOR.total_seconds() + 1)
        async with serving(a_pod(server)) as handled:
            await handed_over(handled, 1)
            return renewal.generation, cut, left, list(handled)

    generation, cut, left, handled = asyncio.run(scenario())

    assert (cut, left) == ([True], 0)
    assert handled == [(_ALICE, generation)]


def the_facts_key(namespaced: dict[str, object]) -> str:
    """The key whose string is a member's facts, apart from tandem's epoch, also a string."""
    (key,) = [
        key for key, held in namespaced.items() if isinstance(held, bytes) and _reads_as_facts(held)
    ]
    return key


def _reads_as_facts(stored: bytes) -> bool:
    try:
        MemberFacts.model_validate_json(stored)
    except ValueError:
        return False
    return True


def test_facts_renewed_on_one_pod_are_read_on_another_and_a_new_name_replaces_them_with_the_lease(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    renamed = a_member_facts(owner="another owner", name="Alicia")

    async def scenario() -> tuple[Mapping[Principal, MemberFacts], LeaseRenewal, LeaseRenewal]:
        pod_a, pod_b = a_pod(server), a_pod(server)
        first = await pod_a.renew(_ALICE, _FACTS)
        seen = await pod_b.facts([_ALICE])
        second = await pod_b.renew(_ALICE, renamed)
        assert seen == {_ALICE: _FACTS}
        return await pod_a.facts([_ALICE]), first, second

    replaced, first, second = asyncio.run(scenario())

    assert replaced == {_ALICE: renamed}
    assert (second.started, second.generation) == (False, first.generation)


def test_a_batch_answers_the_principals_that_have_facts_and_an_empty_batch_answers_nothing(
    server: FakeServer,
) -> None:
    bob = a_member("bob")

    async def scenario() -> tuple[Mapping[Principal, MemberFacts], Mapping[Principal, MemberFacts]]:
        pod = a_pod(server)
        await pod.renew(_ALICE, _FACTS)
        return await pod.facts([bob, _ALICE, _ALICE]), await pod.facts([])

    batch, empty = asyncio.run(scenario())

    assert batch == {_ALICE: _FACTS}
    assert empty == {}


def test_facts_are_gone_once_a_lease_span_passed_without_a_renewal(
    server: FakeServer, redis_clock: RedisClock
) -> None:
    async def scenario() -> tuple[Mapping[Principal, MemberFacts], Mapping[Principal, MemberFacts]]:
        pod = a_pod(server)
        await pod.renew(_ALICE, _FACTS)
        redis_clock.advance(LEASE_SECONDS - 1)
        near_the_end = await pod.facts([_ALICE])
        redis_clock.advance(2)
        return near_the_end, await pod.facts([_ALICE])

    near_the_end, at_the_end = asyncio.run(scenario())

    assert near_the_end == {_ALICE: _FACTS}
    assert at_the_end == {}


@pytest.mark.parametrize(
    "principal",
    [*UNUSUAL_PRINCIPALS, pytest.param(a_member("a:b\nc", iss="urn:x:y"), id="colon and newline")],
)
@pytest.mark.parametrize(
    "facts",
    [
        pytest.param(a_member_facts(name="\U0001f600 Alice \u00fc"), id="non-ASCII name"),
        pytest.param(a_member_facts(name=""), id="empty name"),
    ],
)
def test_facts_round_trip_exactly_for_unusual_principals_and_names(
    server: FakeServer, principal: Principal, facts: MemberFacts
) -> None:
    async def scenario() -> Mapping[Principal, MemberFacts]:
        pod = a_pod(server)
        await pod.renew(principal, facts)
        return await a_pod(server).facts([principal])

    assert asyncio.run(scenario()) == {principal: facts}


def test_a_facts_key_lies_under_the_agents_namespace_and_lasts_at_most_a_lease_span(
    server: FakeServer,
) -> None:
    async def scenario() -> tuple[dict[str, object], int]:
        await a_pod(server).renew(_ALICE, _FACTS)
        raw = FakeAsyncRedis(server=server)
        namespaced = await namespace_of(raw, _PREFIX)
        return namespaced, await raw.pttl(the_facts_key(namespaced))

    namespaced, remaining_ms = asyncio.run(scenario())

    assert the_facts_key(namespaced).startswith(f"{_PREFIX}-agents:")
    assert 0 < remaining_ms <= LEASE_SECONDS * 1000


def test_a_lease_whose_renewal_lost_its_facts_step_reads_as_holding_without_facts(
    server: FakeServer,
) -> None:
    async def scenario() -> tuple[LeaseStanding, Mapping[Principal, MemberFacts]]:
        pod = a_pod(server)
        with pytest.raises(PlaceStoreError):
            await _a_pod_that_loses_its_connection_after_the_lease_step(server).renew(
                _ALICE, _FACTS
            )
        return await pod.state(_ALICE), await pod.facts([_ALICE])

    standing, facts = asyncio.run(scenario())

    assert standing.holds
    assert facts == {}


def _a_pod_that_loses_its_connection_after_the_lease_step(server: FakeServer) -> RedisPlaceStore:
    client = FakeEvalshaStopsAfter(server, evalshas_that_return=1)
    return RedisPlaceStore(client, key_prefix=_PREFIX)


def test_an_empty_batch_answers_nothing_from_a_store_that_refuses_every_script(
    server: FakeServer,
) -> None:
    pod = RedisPlaceStore(FakeEvalshaStopsAfter(server, evalshas_that_return=0), key_prefix=_PREFIX)

    assert asyncio.run(pod.facts([])) == {}


async def put_in_the_facts_key(server: FakeServer, held: object) -> None:
    """Replaces what a renewal kept at the facts key with ``held``: a string, or a hash."""
    pod = a_pod(server)
    await pod.renew(_ALICE, _FACTS)
    raw = FakeAsyncRedis(server=server)
    key = the_facts_key(await namespace_of(raw, _PREFIX))
    await raw.delete(key)
    if isinstance(held, dict):
        await raw.hset(key, mapping=held)
    else:
        await raw.set(key, held)


@pytest.mark.parametrize(
    "held",
    [
        pytest.param("not json", id="text that is no JSON"),
        pytest.param(b'\xff{"owner": "o", "name": "n"}', id="bytes that are no UTF-8"),
        pytest.param('{"owner": "", "name": "x"}', id="an empty owner"),
        pytest.param('{"owner": "o", "name": "n", "extra": 1}', id="an unknown field"),
        pytest.param({"field": "value"}, id="a hash"),
    ],
)
def test_a_facts_key_holding_no_member_facts_fails_the_batch_read_with_place_store_error(
    server: FakeServer, held: object
) -> None:
    async def scenario() -> None:
        await put_in_the_facts_key(server, held)
        await a_pod(server).facts([_ALICE, a_member("bob")])

    with pytest.raises(PlaceStoreError):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    "trailing", [pytest.param(b"", id="valid UTF-8"), pytest.param(b"\xff", id="no UTF-8")]
)
@pytest.mark.parametrize(
    "decoding",
    [pytest.param(False, id="a raw client"), pytest.param(True, id="a decoding client")],
)
def test_an_unreadable_stored_value_leaves_no_owner_or_name_in_the_error_or_the_log(
    server: FakeServer, log_lines: LogLines, trailing: bytes, decoding: bool
) -> None:
    owner, name = "the-owner-sub", "the-recognisable-name"
    stored = f'{{"owner": "{owner}", "name": "{name}", "extra": 1}}'.encode() + trailing

    async def scenario() -> None:
        await put_in_the_facts_key(server, stored)
        await a_pod(server, decoding=decoding).facts([_ALICE])

    with pytest.raises(PlaceStoreError) as refusal:
        asyncio.run(scenario())

    remains = [
        str(refusal.value),
        repr(refusal.value),
        repr(refusal.value.__cause__),
        repr(refusal.value.__context__),
        log_lines.everything(),
    ]
    assert not [text for text in remains if owner in text or name in text]
