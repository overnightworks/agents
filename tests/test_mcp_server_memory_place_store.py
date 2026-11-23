"""``InMemoryPlaceStore`` as the port promises it: what a subscription is told and when, and what
the listen record answers. The front's reaction to a subscription is in
``test_mcp_server_place_wake``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from agents.mcp_server import InMemoryPlaceStore
from agents.mcp_server.place_store import (
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    ToolsChange,
)
from agents.member import STANDING_URI
from tests.support.mcp_server_support import a_member, settled
from tests.support.place_store_support import lapsing
from tests.support.virtual_time import in_virtual_time

_READY_WITHIN = timedelta(seconds=10)
_ALICE = a_member("alice")
_BOB = a_member("bob")
_HOLDS = timedelta(seconds=30)
_EVERY = timedelta(seconds=1)
_ANOTHER_POD = "another-pod"


@dataclass
class Following:
    """What one subscription was called with, in order, and the failures it was told of."""

    store: InMemoryPlaceStore
    calls: list[PlaceSignal | ListeningTurn | str] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)
    raise_in_resync: Exception | None = None
    raise_on_signal: Exception | None = None

    def on_resync(self) -> None:
        self.calls.append("resync")
        if self.raise_in_resync is not None:
            raise self.raise_in_resync

    def on_signal(self, signal: PlaceSignal | ListeningTurn) -> None:
        self.calls.append(signal)
        if self.raise_on_signal is not None:
            raise self.raise_on_signal

    @asynccontextmanager
    async def open(self, ready_within: timedelta = _READY_WITHIN) -> AsyncIterator[None]:
        async with self.store.following(
            on_resync=self.on_resync,
            on_signal=self.on_signal,
            on_failed=self.failures.append,
            ready_within=ready_within,
        ):
            yield


def test_entering_returns_once_the_subscription_is_confirmed_and_on_resync_has_returned() -> None:
    following = Following(InMemoryPlaceStore())

    async def scenario() -> list[PlaceSignal | ListeningTurn | str]:
        async with following.open():
            return list(following.calls)

    assert in_virtual_time(scenario()) == ["resync"]


def test_entering_raises_place_store_error_once_ready_within_passes_without_a_confirmation() -> (
    None
):
    following = Following(InMemoryPlaceStore(confirms_subscription=False))

    async def scenario() -> float:
        started = asyncio.get_running_loop().time()
        entering = following.open(timedelta(seconds=10))
        with pytest.raises(PlaceStoreError):
            async with entering:
                pytest.fail("entered without a confirmation")
        return asyncio.get_running_loop().time() - started

    assert in_virtual_time(scenario()) == pytest.approx(10)
    assert following.calls == []


def test_a_signal_published_while_the_subscription_is_unconfirmed_is_never_handled() -> None:
    store = InMemoryPlaceStore(confirms_subscription=False)
    following = Following(store)

    async def scenario() -> None:
        publishing = asyncio.create_task(store.publish(ToolsChange(_ALICE, _ANOTHER_POD)))
        entering = following.open()
        with pytest.raises(PlaceStoreError):
            async with entering:
                pytest.fail("entered without a confirmation")
        await publishing

    in_virtual_time(scenario())

    assert following.calls == []


@pytest.mark.parametrize("ready_within", [timedelta(0), timedelta(seconds=-1)])
def test_entering_with_a_ready_within_that_is_not_positive_raises_value_error(
    ready_within: timedelta,
) -> None:
    following = Following(InMemoryPlaceStore())

    async def scenario() -> None:
        async with following.open(ready_within):
            pytest.fail("entered with no time to confirm")

    entering = scenario()
    with pytest.raises(ValueError, match="ready_within"):
        in_virtual_time(entering)


def test_every_reconfirmation_calls_on_resync_again() -> None:
    store = InMemoryPlaceStore()
    following = Following(store)

    async def scenario() -> None:
        async with following.open():
            store.resubscribe()
            await settled()

    in_virtual_time(scenario())

    assert following.calls == ["resync", "resync"]


def test_a_signal_reaches_every_subscription_the_publisher_s_included_in_publishing_order() -> None:
    store = InMemoryPlaceStore()
    here, there = Following(store), Following(store)
    changes = [ResourceChange(_ALICE, STANDING_URI, _ANOTHER_POD), ToolsChange(_BOB, _ANOTHER_POD)]

    async def scenario() -> None:
        async with here.open(), there.open():
            for change in changes:
                await store.publish(change)
            await settled()

    in_virtual_time(scenario())

    assert (here.calls, there.calls) == (["resync", *changes], ["resync", *changes])


def test_the_turns_of_the_listen_record_reach_the_subscriptions_in_the_order_they_turned() -> None:
    store = InMemoryPlaceStore()
    following = Following(store)

    async def scenario() -> None:
        async with following.open():
            await store.acknowledged("first", _ALICE, [STANDING_URI], _HOLDS)
            await store.acknowledged("second", _ALICE, [STANDING_URI], _HOLDS)
            async with lapsing(store, every=_EVERY):
                await asyncio.sleep(_HOLDS.total_seconds() + _EVERY.total_seconds())
            await settled()

    in_virtual_time(scenario())

    assert following.calls == [
        "resync",
        ListeningTurn(_ALICE, STANDING_URI, listens=True),
        ListeningTurn(_ALICE, STANDING_URI, listens=False),
    ]


def test_a_handler_that_raises_after_the_entry_is_called_on_failed_once_and_handles_no_more() -> (
    None
):
    store = InMemoryPlaceStore()
    following = Following(store)
    refusal = RuntimeError("the host's handler failed")
    following.raise_on_signal = refusal
    first = ToolsChange(_ALICE, _ANOTHER_POD)

    async def scenario() -> None:
        async with following.open():
            await store.publish(first)
            await store.publish(ToolsChange(_BOB, _ANOTHER_POD))
            await settled()
            following.calls.append("still entered")

    in_virtual_time(scenario())

    assert following.failures == [refusal]
    assert following.calls == ["resync", first, "still entered"]


def test_a_handler_that_raises_before_the_entry_returns_is_raised_out_of_the_entry() -> None:
    following = Following(InMemoryPlaceStore())
    following.raise_in_resync = RuntimeError("the host's resync failed")

    async def scenario() -> None:
        async with following.open():
            pytest.fail("entered although the first resync raised")

    entering = scenario()
    with pytest.raises(RuntimeError, match="the host's resync failed"):
        in_virtual_time(entering)

    assert following.failures == []


def test_a_timeout_error_from_a_handler_before_the_entry_is_raised_as_it_is() -> None:
    following = Following(InMemoryPlaceStore())
    following.raise_in_resync = TimeoutError("the host's resync timed out")

    async def scenario() -> None:
        async with following.open():
            pytest.fail("entered although the first resync raised")

    entering = scenario()
    with pytest.raises(TimeoutError, match="the host's resync timed out") as raised:
        in_virtual_time(entering)

    assert not isinstance(raised.value, PlaceStoreError)


def test_a_subscription_that_fails_after_the_entry_calls_on_failed_once_with_the_failure() -> None:
    store = InMemoryPlaceStore()
    following = Following(store)
    refusal = PlaceStoreError("the subscription was refused")

    async def scenario() -> None:
        async with following.open():
            store.fail_subscriptions(refusal)
            await settled()

    in_virtual_time(scenario())

    assert following.failures == [refusal]


def test_a_signal_is_handled_only_once_the_signals_are_released() -> None:
    store = InMemoryPlaceStore()
    following = Following(store)
    change = ToolsChange(_ALICE, _ANOTHER_POD)

    async def scenario() -> list[int]:
        async with following.open():
            store.hold_signals()
            await store.publish(change)
            await settled()
            held = len(following.calls)
            store.release_signals()
            await settled()
            return [held, len(following.calls)]

    assert in_virtual_time(scenario()) == [1, 2]


def test_a_stream_is_listened_to_until_it_ends_and_its_allowance_runs_out() -> None:
    store = InMemoryPlaceStore()

    async def scenario() -> list[object]:
        yes = await store.acknowledged("stream", _ALICE, [STANDING_URI], _HOLDS)
        replay = await store.acknowledged("stream", _ALICE, [STANDING_URI], _HOLDS)
        answers: list[object] = [yes, replay, await store.listens_to(_ALICE, STANDING_URI)]
        answers.append(await store.listeners(STANDING_URI))
        await store.ended("stream", timedelta(seconds=5))
        await store.ended("stream", _HOLDS)
        async with lapsing(store, every=_EVERY) as turns:
            await asyncio.sleep(5 + _EVERY.total_seconds())
        answers += [turns, await store.listens_to(_ALICE, STANDING_URI)]
        return answers

    assert in_virtual_time(scenario()) == [
        frozenset({STANDING_URI}),
        frozenset(),
        True,
        frozenset({_ALICE}),
        [ListeningTurn(_ALICE, STANDING_URI, listens=False)],
        False,
    ]
