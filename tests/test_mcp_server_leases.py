"""``MemberLeases``: what a host calls to keep, read and list a member's lease, and the lapse
service it runs in its lifespan."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest

from agents.mcp_server import MemberLeases
from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.leases import LAPSE_RESTART_SECONDS
from agents.mcp_server.place_store import (
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    PlaceStoreError,
)
from agents.member import LEASE_SECONDS
from agents.turn import Principal
from tests.support.mcp_server_support import a_member
from tests.support.place_store_support import LeaseRecordFake
from tests.support.virtual_time import in_virtual_time

_PAST_THE_LEASE = LEASE_SECONDS + 2


class LapseLog:
    """The lapses a host's ``lease_ended`` received, and what it raises when told to."""

    def __init__(self) -> None:
        self.received: list[tuple[Principal, str]] = []
        self.raises: Exception | None = None

    async def __call__(self, principal: Principal, generation: str) -> None:
        self.received.append((principal, generation))
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def alice() -> Principal:
    return a_member("alice")


@pytest.fixture
def record() -> LeaseRecordFake:
    return LeaseRecordFake()


@pytest.fixture
def lapses() -> LapseLog:
    return LapseLog()


@pytest.fixture
def leases(record: LeaseRecordFake, lapses: LapseLog) -> MemberLeases:
    return MemberLeases(record, lease_ended=lapses)


def test_renew_state_and_live_answer_what_the_store_holds(
    leases: MemberLeases, alice: Principal
) -> None:
    async def scenario() -> tuple[LeaseRenewal, LeaseRenewal, LeaseStanding, LeaseRoster]:
        first = await leases.renew(alice)
        second = await leases.renew(alice)
        return first, second, await leases.state(alice), await leases.live()

    first, second, standing, roster = in_virtual_time(scenario())

    assert (first.started, second.started) == (True, False)
    assert second.generation == first.generation
    assert standing == LeaseStanding(holds=True, generation=first.generation)
    assert roster == LeaseRoster(holders=frozenset({alice}), settled=True)


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda leases, principal: leases.renew(principal), id="renew"),
        pytest.param(lambda leases, principal: leases.state(principal), id="state"),
        pytest.param(lambda leases, _principal: leases.live(), id="live"),
    ],
)
def test_a_failing_store_raises_place_store_error_from_each_call(
    leases: MemberLeases,
    record: LeaseRecordFake,
    alice: Principal,
    read: Callable[[MemberLeases, Principal], Awaitable[object]],
) -> None:
    record.failure = PlaceStoreError("the store did not answer")
    lease_call = read(leases, alice)

    with pytest.raises(PlaceStoreError):
        in_virtual_time(lease_call)


def test_a_lapse_reaches_lease_ended_with_the_principal_and_the_generation_it_lapsed_with(
    leases: MemberLeases, lapses: LapseLog, alice: Principal
) -> None:
    async def scenario() -> str:
        async with leases.running():
            renewal = await leases.renew(alice)
            await asyncio.sleep(_PAST_THE_LEASE)
            return renewal.generation

    generation = in_virtual_time(scenario())

    assert lapses.received == [(alice, generation)]


def test_a_lease_ended_that_raises_reaches_the_store_and_is_not_swallowed(
    leases: MemberLeases, record: LeaseRecordFake, lapses: LapseLog, alice: Principal
) -> None:
    lapses.raises = RuntimeError("the host failed")

    async def scenario() -> None:
        async with leases.running():
            await leases.renew(alice)
            await asyncio.sleep(_PAST_THE_LEASE)

    in_virtual_time(scenario())

    assert record.handler_failures
    assert all(failure is lapses.raises for failure in record.handler_failures)
    assert len(record.serve_calls) == 1


def test_a_lapse_service_that_fails_is_logged_by_type_and_serves_again_after_the_pause(
    leases: MemberLeases, record: LeaseRecordFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    refusal = PlaceStoreError("the secret detail")
    refusal.__cause__ = PermissionError("no permission")
    record.serve_failures = [refusal]

    async def scenario() -> tuple[int, bool]:
        async with leases.running():
            await asyncio.sleep(0)
            before = len(record.serve_calls)
            await asyncio.sleep(LAPSE_RESTART_SECONDS + 1)
            return before, record.serving

    before, serving = in_virtual_time(scenario())

    assert (before, len(record.serve_calls), serving) == (1, 2, True)
    logged = " ".join(entry.getMessage() for entry in caplog.records)
    assert "PermissionError" in logged
    assert "secret detail" not in logged


def test_an_unexpected_failure_of_the_lapse_service_is_logged_with_its_traceback_once(
    leases: MemberLeases, record: LeaseRecordFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    record.serve_failures = [RuntimeError("one"), RuntimeError("two"), RuntimeError("three")]

    async def scenario() -> None:
        async with leases.running():
            await asyncio.sleep(3 * LAPSE_RESTART_SECONDS)

    in_virtual_time(scenario())

    failures = [entry for entry in caplog.records if entry.exc_info is not None]
    assert len(failures) == 1
    assert "RuntimeError" in failures[0].getMessage()


def test_a_failure_after_a_run_longer_than_the_pause_is_logged_again(
    leases: MemberLeases, record: LeaseRecordFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    record.serve_failures = [RuntimeError("first streak")]

    async def scenario() -> None:
        async with leases.running():
            await asyncio.sleep(2.5 * LAPSE_RESTART_SECONDS)
            record.fail_running = RuntimeError("second streak")
            await asyncio.sleep(LAPSE_RESTART_SECONDS)

    in_virtual_time(scenario())

    assert len([entry for entry in caplog.records if entry.exc_info is not None]) == 2


def test_leaving_running_cancels_the_lapse_service_and_waits_for_it(
    leases: MemberLeases, record: LeaseRecordFake
) -> None:
    async def scenario() -> tuple[bool, bool, int]:
        async with leases.running():
            await asyncio.sleep(0)
            inside = record.serving
        return inside, record.serving, len(asyncio.all_tasks()) - 1

    assert in_virtual_time(scenario()) == (True, False, 0)
