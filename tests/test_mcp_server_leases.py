"""``MemberLeases``: what a host calls to keep, read and list a member's lease, and the lapse
service it runs in its lifespan."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from agents.mcp_server import MemberLeases
from agents.mcp_server.leases import LAPSE_RESTART_SECONDS
from agents.mcp_server.place_store import (
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    PlaceStoreError,
)
from agents.member import LEASE_SECONDS
from agents.turn import Principal
from tests.support.log_lines import LogLines, log_lines
from tests.support.mcp_server_support import a_member
from tests.support.place_store_support import LeaseRecordFake
from tests.support.virtual_time import in_virtual_time

_PAST_THE_LEASE = LEASE_SECONDS + 2

log_lines = log_lines


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
    ("read", "event"),
    [
        pytest.param(
            lambda leases, principal: leases.renew(principal), "lease_renewal.ended", id="renew"
        ),
        pytest.param(
            lambda leases, principal: leases.state(principal),
            "lease_state_lookup.ended",
            id="state",
        ),
        pytest.param(
            lambda leases, _principal: leases.live(), "lease_roster_lookup.ended", id="live"
        ),
    ],
)
def test_a_failing_store_raises_place_store_error_from_each_call_and_writes_one_failed_line(
    leases: MemberLeases,
    record: LeaseRecordFake,
    alice: Principal,
    log_lines: LogLines,
    read: Callable[[MemberLeases, Principal], Awaitable[object]],
    event: str,
) -> None:
    refusal = PlaceStoreError("the store did not answer")
    refusal.__cause__ = PermissionError("no permission")
    record.failure = refusal
    lease_call = read(leases, alice)

    with pytest.raises(PlaceStoreError):
        in_virtual_time(lease_call)

    failed = log_lines.one(event)
    assert (failed["outcome"], failed["error_type"], failed["cause_type"]) == (
        "failed",
        "PlaceStoreError",
        "PermissionError",
    )
    assert log_lines.of("member_lease.opened") == []


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


def test_a_lapse_service_that_fails_writes_one_error_line_by_type_and_serves_again_after_the_pause(
    leases: MemberLeases, record: LeaseRecordFake, log_lines: LogLines
) -> None:
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
    failed = log_lines.one("lease_lapse_service.failed")
    assert (failed["level"], failed["error_type"]) == ("error", "PermissionError")
    assert "exception" not in failed
    assert "secret detail" not in log_lines.everything()


def test_an_unexpected_failure_of_the_lapse_service_is_written_with_its_traceback_once_per_streak(
    leases: MemberLeases, record: LeaseRecordFake, log_lines: LogLines
) -> None:
    record.serve_failures = [RuntimeError("one"), RuntimeError("two"), RuntimeError("three")]

    async def scenario() -> None:
        async with leases.running():
            await asyncio.sleep(3 * LAPSE_RESTART_SECONDS)

    in_virtual_time(scenario())

    failed = log_lines.one("lease_lapse_service.failed")
    assert failed["error_type"] == "RuntimeError"
    assert "exception" in failed


def test_a_failure_after_a_run_longer_than_the_pause_is_written_again(
    leases: MemberLeases, record: LeaseRecordFake, log_lines: LogLines
) -> None:
    record.serve_failures = [RuntimeError("first streak")]

    async def scenario() -> None:
        async with leases.running():
            await asyncio.sleep(2.5 * LAPSE_RESTART_SECONDS)
            record.fail_running = RuntimeError("second streak")
            await asyncio.sleep(LAPSE_RESTART_SECONDS)

    in_virtual_time(scenario())

    assert len(log_lines.of("lease_lapse_service.failed")) == 2


def test_a_renewal_is_one_line_and_only_a_start_writes_an_opened_line_with_the_sub(
    leases: MemberLeases, alice: Principal, log_lines: LogLines
) -> None:
    async def scenario() -> LeaseRenewal:
        first = await leases.renew(alice)
        await leases.renew(alice)
        return first

    first = in_virtual_time(scenario())

    renewals = log_lines.of("lease_renewal.ended")
    assert [line["outcome"] for line in renewals] == ["ok", "ok"]
    assert log_lines.one("member_lease.opened")["sub"] == alice.sub
    assert first.generation not in log_lines.everything()


def test_a_state_and_a_roster_read_are_one_line_each_with_the_count_of_holders(
    leases: MemberLeases, alice: Principal, log_lines: LogLines
) -> None:
    async def scenario() -> None:
        await leases.renew(alice)
        await leases.state(alice)
        await leases.live()

    in_virtual_time(scenario())

    assert log_lines.one("lease_state_lookup.ended")["outcome"] == "ok"
    roster = log_lines.one("lease_roster_lookup.ended")
    assert (roster["outcome"], roster["holder_count"]) == ("ok", 1)


def test_a_lapse_writes_a_lapsed_line_with_the_sub_and_never_the_generation(
    leases: MemberLeases, alice: Principal, log_lines: LogLines
) -> None:
    async def scenario() -> str:
        async with leases.running():
            renewal = await leases.renew(alice)
            await asyncio.sleep(_PAST_THE_LEASE)
            return renewal.generation

    generation = in_virtual_time(scenario())

    assert log_lines.one("member_lease.lapsed")["sub"] == alice.sub
    assert generation not in log_lines.everything()


def test_leaving_running_cancels_the_lapse_service_and_waits_for_it(
    leases: MemberLeases, record: LeaseRecordFake
) -> None:
    async def scenario() -> tuple[bool, bool, int]:
        async with leases.running():
            await asyncio.sleep(0)
            inside = record.serving
        return inside, record.serving, len(asyncio.all_tasks()) - 1

    assert in_virtual_time(scenario()) == (True, False, 0)
