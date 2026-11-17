"""Test builders around the in-memory ports: a place store that fails on demand, and a lease record.

``PlaceStoreFake`` is the package's ``InMemoryPlaceStore`` with the faults a suite makes it show.
``LeaseRecordFake`` is a ``LeaseRecord``. Time is the running loop's, so the virtual clock moves a
stream's or a lease's deadline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import NoReturn

from agents.mcp_server import InMemoryPlaceStore
from agents.mcp_server.place_store import (
    LapseRun,
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
)
from agents.member import LEASE_SECONDS
from agents.turn import Principal


def refused_with_a_cause() -> PlaceStoreError:
    """The failure a store library's refusal becomes: a ``PlaceStoreError`` over its cause."""
    refusal = PlaceStoreError("the secret detail")
    refusal.__cause__ = PermissionError("the secret detail")
    return refusal


class PlaceStoreFake(InMemoryPlaceStore):
    """An ``InMemoryPlaceStore`` that fails the calls a test names.

    ``publish_failure`` makes every publish raise; ``ended_takes`` is the seconds the record's
    ``ended`` waits before it answers. A ``*_failure`` raises from that call of the record;
    ``acknowledged_failure`` raises after the acknowledgement committed and published, the way a
    lost reply does. ``published`` holds the signals published and ``stream_calls`` the calls a
    stream made to the record. ``signal_tasks`` are the tasks that handle the subscriptions'
    signals, which tells a host's callable which task called it.
    """

    def __init__(self, *, confirms_subscription: bool = True) -> None:
        super().__init__(confirms_subscription=confirms_subscription)
        self.publish_failure: Exception | None = None
        self.acknowledged_failure: PlaceStoreError | None = None
        self.ended_failure: PlaceStoreError | None = None
        self.ended_takes: float = 0
        self.lapse_failure: Exception | None = None
        self.lapse_item_failure: str | None = None
        self.read_failure: PlaceStoreError | None = None
        self.published: list[PlaceSignal] = []
        self.stream_calls: list[str] = []
        self.signal_tasks: set[asyncio.Task[object]] = set()

    async def publish(self, signal: PlaceSignal) -> None:
        if self.publish_failure is not None:
            raise self.publish_failure
        self.published.append(signal)
        await super().publish(signal)

    @asynccontextmanager
    async def following(
        self,
        *,
        on_resync: Callable[[], None],
        on_signal: Callable[[PlaceSignal | ListeningTurn], None],
        on_failed: Callable[[Exception], None],
        ready_within: timedelta,
    ) -> AsyncGenerator[None]:
        def resynced() -> None:
            task = asyncio.current_task()
            assert task is not None
            self.signal_tasks.add(task)
            on_resync()

        async with super().following(
            on_resync=resynced,
            on_signal=on_signal,
            on_failed=on_failed,
            ready_within=ready_within,
        ):
            yield

    async def acknowledged(
        self, stream: str, principal: Principal, uris: Collection[str], holds_for: timedelta
    ) -> frozenset[str]:
        self.stream_calls.append("acknowledged")
        turned = await super().acknowledged(stream, principal, uris, holds_for)
        if self.acknowledged_failure is not None:
            raise self.acknowledged_failure
        return turned

    async def ended(self, stream: str, holds_for: timedelta) -> None:
        if self.ended_takes:
            await asyncio.sleep(self.ended_takes)
        self.stream_calls.append("ended")
        if self.ended_failure is not None:
            raise self.ended_failure
        await super().ended(stream, holds_for)

    async def listens_to(self, principal: Principal, uri: str) -> bool:
        if self.read_failure is not None:
            raise self.read_failure
        return await super().listens_to(principal, uri)

    async def listeners(self, uri: str) -> frozenset[Principal]:
        if self.read_failure is not None:
            raise self.read_failure
        return await super().listeners(uri)

    async def lapse_due(self, claimer: str) -> LapseRun:
        if self.lapse_failure is not None:
            raise self.lapse_failure
        if self.lapse_item_failure is not None:
            return LapseRun(failed_count=1, last_error_type=self.lapse_item_failure)
        return await super().lapse_due(claimer)


@dataclass
class LeaseRecordFake:
    """A ``LeaseRecord`` whose leases last ``LEASE_SECONDS`` of the loop's time.

    ``serve_failures`` are raised by the next calls of ``serve_lapses``, one each; ``fail_running``
    is raised by the call that runs, at its next round. A ``handle`` that
    raises is recorded in ``handler_failures`` and its lapse stays due, as the store gives it again.
    ``failure`` makes ``renew``, ``state`` and ``live`` raise. ``serving`` is true while a call of
    ``serve_lapses`` runs, and ``serve_calls`` holds the claimer of each.
    """

    failure: PlaceStoreError | None = None
    serve_failures: list[Exception] = field(default_factory=list)
    fail_running: Exception | None = None
    handler_failures: list[Exception] = field(default_factory=list)
    serve_calls: list[str] = field(default_factory=list)
    serving: bool = False
    _leases: dict[Principal, tuple[float, str]] = field(default_factory=dict)
    _lapses: list[tuple[Principal, str]] = field(default_factory=list)
    _starts: int = 0

    async def renew(self, principal: Principal) -> LeaseRenewal:
        self._reads()
        now = asyncio.get_running_loop().time()
        held = self._leases.get(principal)
        started = held is None or held[0] <= now
        if started:
            self._starts += 1
            generation = f"generation-{self._starts}"
            if held is not None:
                self._lapses.append((principal, held[1]))
        else:
            generation = held[1]
        self._leases[principal] = (now + LEASE_SECONDS, generation)
        return LeaseRenewal(started=started, generation=generation)

    async def state(self, principal: Principal) -> LeaseStanding:
        self._reads()
        held = self._leases.get(principal)
        if held is None or held[0] <= asyncio.get_running_loop().time():
            return LeaseStanding(holds=False, generation=None)
        return LeaseStanding(holds=True, generation=held[1])

    async def live(self) -> LeaseRoster:
        self._reads()
        now = asyncio.get_running_loop().time()
        return LeaseRoster(
            holders=frozenset(p for p, (due, _) in self._leases.items() if due > now), settled=True
        )

    async def serve_lapses(
        self, handle: Callable[[Principal, str], Awaitable[None]], *, claimer: str
    ) -> NoReturn:
        self.serve_calls.append(claimer)
        if self.serve_failures:
            raise self.serve_failures.pop(0)
        self.serving = True
        try:
            while True:
                self._sweep()
                for lapse in list(self._lapses):
                    try:
                        await handle(*lapse)
                    except Exception as failure:
                        self.handler_failures.append(failure)
                    else:
                        self._lapses.remove(lapse)
                await asyncio.sleep(1)
                if self.fail_running is not None:
                    raise self.fail_running
        finally:
            self.serving = False

    def _reads(self) -> None:
        if self.failure is not None:
            raise self.failure

    def _sweep(self) -> None:
        now = asyncio.get_running_loop().time()
        for principal, (due, generation) in list(self._leases.items()):
            if due <= now:
                del self._leases[principal]
                self._lapses.append((principal, generation))
