"""In-memory ports for the suites that run ``ResourceChanges`` and ``MemberLeases`` without a store.

``SharedPlaceStore`` is what every pod of an app shares; each pod holds its own ``PlaceStoreFake``
view of it. ``LeaseRecordFake`` is a ``LeaseRecord``. Time is the running loop's, so the virtual
clock moves a stream's or a lease's deadline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import timedelta
from typing import NoReturn

from agents.mcp_server.place_store import (
    LapseRun,
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    Resubscribed,
    ToolsChange,
)
from agents.member import LEASE_SECONDS
from agents.turn import Principal

_ENDED = object()

type _Event = PlaceSignal | ListeningTurn | Resubscribed


@dataclass
class _StreamRecord:
    principal: Principal
    uris: frozenset[str]
    due: float


@dataclass
class SharedPlaceStore:
    """The streams that honored a URI, each with its deadline, the views that hear a publish and the
    calls a stream made to the record (``stream_calls``)."""

    streams: dict[str, _StreamRecord] = field(default_factory=dict)
    views: list[PlaceStoreFake] = field(default_factory=list)
    stream_calls: list[str] = field(default_factory=list)

    def pod(self) -> PlaceStoreFake:
        return PlaceStoreFake(shared=self)

    def broadcast(self, event: _Event) -> None:
        for view in self.views:
            view.deliver(event)

    def listeners(self, uri: str) -> frozenset[Principal]:
        return frozenset(r.principal for r in self.streams.values() if uri in r.uris)

    def lapse_due(self, now: float) -> LapseRun:
        due = [name for name, record in self.streams.items() if record.due <= now]
        turns: list[ListeningTurn] = []
        for stream in due:
            record = self.streams.pop(stream)
            for uri in record.uris:
                if record.principal not in self.listeners(uri):
                    turns.append(ListeningTurn(record.principal, uri, listens=False))
                    self.broadcast(turns[-1])
        return LapseRun(turns=tuple(turns), stream_count=len(due))


@dataclass
class PlaceStoreFake:
    """One pod's view of the store: a published signal comes back through ``received``, as it does
    from a real subscription of the publishing pod.

    ``received`` opens with a ``Resubscribed`` unless ``confirms_subscription`` is false, which a
    test then sends with ``resubscribe``. ``fail`` and ``end`` make ``received`` raise or finish
    once the events before them were read; ``publish_failure`` makes every publish raise;
    ``ended_takes`` is the seconds the record's ``ended`` waits before it answers. A
    ``*_failure`` raises from that call of the record; ``acknowledged_failure`` raises after the
    acknowledgement committed and published, the way a lost reply does. ``hold_signals`` keeps
    ``received`` from yielding until ``release_signals``. ``signal_task`` is the task that reads
    ``received``, which tells a host's callable which task called it.
    """

    shared: SharedPlaceStore = field(default_factory=SharedPlaceStore)
    confirms_subscription: bool = True
    publish_failure: Exception | None = None
    acknowledged_failure: PlaceStoreError | None = None
    ended_failure: PlaceStoreError | None = None
    ended_takes: float = 0
    lapse_failure: Exception | None = None
    lapse_item_failure: str | None = None
    read_failure: PlaceStoreError | None = None
    published: list[PlaceSignal] = field(default_factory=list)
    signal_task: asyncio.Task[object] | None = None
    _events: asyncio.Queue[_Event | Exception | object] = field(default_factory=asyncio.Queue)
    _released: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._released.set()
        self.shared.views.append(self)

    async def publish(self, signal: PlaceSignal) -> None:
        if self.publish_failure is not None:
            raise self.publish_failure
        self.published.append(signal)
        self.shared.broadcast(signal)

    async def received(self) -> AsyncIterator[_Event]:
        self.signal_task = asyncio.current_task()
        if self.confirms_subscription:
            yield Resubscribed()
        while True:
            event = await self._events.get()
            await self._released.wait()
            if event is _ENDED:
                return
            if isinstance(event, Exception):
                raise event
            assert isinstance(event, ResourceChange | ToolsChange | ListeningTurn | Resubscribed)
            yield event

    async def acknowledged(
        self, stream: str, principal: Principal, uris: Collection[str], holds_for: timedelta
    ) -> frozenset[str]:
        self.shared.stream_calls.append("acknowledged")
        turned: frozenset[str] = frozenset()
        if stream not in self.shared.streams:
            turned = frozenset(uri for uri in uris if principal not in self.shared.listeners(uri))
            due = asyncio.get_running_loop().time() + holds_for.total_seconds()
            self.shared.streams[stream] = _StreamRecord(principal, frozenset(uris), due)
            for uri in sorted(turned):
                self.shared.broadcast(ListeningTurn(principal, uri, listens=True))
        if self.acknowledged_failure is not None:
            raise self.acknowledged_failure
        return turned

    async def ended(self, stream: str, holds_for: timedelta) -> None:
        if self.ended_takes:
            await asyncio.sleep(self.ended_takes)
        self.shared.stream_calls.append("ended")
        if self.ended_failure is not None:
            raise self.ended_failure
        record = self.shared.streams.get(stream)
        shortened = asyncio.get_running_loop().time() + holds_for.total_seconds()
        if record is not None and record.due > shortened:
            record.due = shortened

    async def listens_to(self, principal: Principal, uri: str) -> bool:
        if self.read_failure is not None:
            raise self.read_failure
        return principal in self.shared.listeners(uri)

    async def listeners(self, uri: str) -> frozenset[Principal]:
        if self.read_failure is not None:
            raise self.read_failure
        return self.shared.listeners(uri)

    async def lapse_due(self, claimer: str) -> LapseRun:
        del claimer
        if self.lapse_failure is not None:
            raise self.lapse_failure
        if self.lapse_item_failure is not None:
            return LapseRun(failed_count=1, last_error_type=self.lapse_item_failure)
        return self.shared.lapse_due(asyncio.get_running_loop().time())

    def hold_signals(self) -> None:
        self._released.clear()

    def release_signals(self) -> None:
        self._released.set()

    def resubscribe(self) -> None:
        self._events.put_nowait(Resubscribed())

    def deliver(self, event: _Event) -> None:
        """A signal or a turn another pod published."""
        self._events.put_nowait(event)

    def fail(self, failure: Exception) -> None:
        self._events.put_nowait(failure)

    def end(self) -> None:
        self._events.put_nowait(_ENDED)


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
