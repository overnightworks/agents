"""The ports to the store that every pod of an app shares; no module here imports a store library.

``PlaceSignals`` carries an address, never content: a wake names the member and the URI or the
tool list that changed, and the pod that receives it reads the app's own truth. A listening turn
carries its direction as well. ``ListenRecord`` keeps which principal listens to which URI, on
every pod alike. ``LeaseRecord`` keeps a member's lease, which survives the pod that renewed it. A
host that runs more than one pod gives ``ResourceChanges`` and ``MemberLeases`` the implementation
in ``agents.mcp_server.redis_place_store``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn, Protocol

from logline import StepTimer

from agents.turn import Principal


class PlaceStoreError(Exception):
    """The store refused or did not answer; whether a publish arrived is uncertain."""


def failure_type(failure: Exception) -> str:
    """The type name a log line carries for a failure: the innermost cause of a
    ``PlaceStoreError``, which is what the store library met (a lost connection, a refusal), else
    the failure's own type; never its text."""
    if not isinstance(failure, PlaceStoreError):
        return type(failure).__name__
    root: BaseException = failure
    while root.__cause__ is not None:
        root = root.__cause__
    return type(root).__name__


@asynccontextmanager
async def store_step(
    event: str, logger: logging.Logger, **fields: int | str
) -> AsyncGenerator[StepTimer, None]:
    """One timed call of the place store: the timer's line, and on a failure the line also carries
    ``cause_type``, the name ``failure_type`` gives, since the timer's ``error_type`` is the class
    of the ``PlaceStoreError`` itself."""
    timer = StepTimer(event, logger)
    timer.add(**fields)
    async with timer as step:
        try:
            yield step
        except Exception as failure:
            step.add(cause_type=failure_type(failure))
            raise


@dataclass(frozen=True)
class ResourceChange:
    """The resource at ``uri`` of ``principal`` changed; ``origin`` names the publishing pod.

    ``traceparent`` is the publisher's trace as received, ``None`` when it had none; a publisher
    sets none, the store sends the caller's current trace.
    """

    principal: Principal
    uri: str
    origin: str
    traceparent: str | None = None


@dataclass(frozen=True)
class ToolsChange:
    """The tools offered to ``principal`` changed; ``origin`` names the publishing pod; the
    ``traceparent`` is a ``ResourceChange``'s."""

    principal: Principal
    origin: str
    traceparent: str | None = None


type PlaceSignal = ResourceChange | ToolsChange


@dataclass(frozen=True)
class ListeningTurn:
    """The answer to whether ``principal`` listens to ``uri`` turned to ``listens``; only the
    record publishes one, in the trace of the call that turned it (``traceparent`` is a
    ``ResourceChange``'s)."""

    principal: Principal
    uri: str
    listens: bool
    traceparent: str | None = None


@dataclass(frozen=True)
class LapseRun:
    """What one ``lapse_due`` did: the ``turns`` it published, the streams it ``stream_count``
    lapsed, and the streams it could not (``failed_count``), with the type name of the last
    failure. A run that claimed nothing is all zeros and ``None``."""

    turns: tuple[ListeningTurn, ...] = ()
    stream_count: int = 0
    failed_count: int = 0
    last_error_type: str | None = None


@dataclass(frozen=True)
class Resubscribed:
    """The subscription is confirmed, the first time and after every reconnect: a signal sent
    while it was down is lost, so every pod re-reads what it serves."""


class PlaceSignals(Protocol):
    async def publish(self, signal: PlaceSignal) -> None:
        """Sends the signal to every pod subscribed, this one included.

        Raises ``PlaceStoreError`` when the store refuses or does not answer; the delivery is then
        uncertain and nothing is retried.
        """
        ...

    def received(self) -> AsyncIterator[PlaceSignal | ListeningTurn | Resubscribed]:
        """The confirmation of every subscription and each signal and turn after it, until
        cancelled.

        Raises ``PlaceStoreError`` when the subscription cannot recover.
        """
        ...


class ListenRecord(Protocol):
    """Which principal listens to which URI: the streams that honored a URI, each with a deadline
    on the store's clock. Every method raises ``PlaceStoreError`` when the store refuses or does
    not answer."""

    async def acknowledged(
        self, stream: str, principal: Principal, uris: Collection[str], holds_for: timedelta
    ) -> frozenset[str]:
        """Records the stream, which honored ``uris``, until ``holds_for`` from now; returns the
        URIs this call turned to yes, which are none for a stream already recorded.

        A call that raises may have committed, and published its turns.
        """
        ...

    async def ended(self, stream: str, holds_for: timedelta) -> None:
        """Shortens the stream's deadline to ``holds_for`` from now, never lengthens it."""
        ...

    async def listens_to(self, principal: Principal, uri: str) -> bool:
        """Whether a recorded stream of the principal honored the URI."""
        ...

    async def listeners(self, uri: str) -> frozenset[Principal]:
        """The principals with a recorded stream that honored the URI."""
        ...

    async def lapse_due(self, claimer: str) -> LapseRun:
        """Lapses every stream whose deadline passed that nobody else lapses now; a principal's
        last stream of a URI turns it to no, publishes a ``ListeningTurn`` and is one of the
        run's ``turns``, which this call's pod alone returns. A stream that fails to lapse is
        counted, not raised, and its claim runs out."""
        ...


@dataclass(frozen=True)
class LeaseRenewal:
    """The answer to a renewal: ``started`` when a new lease began, and its ``generation``, a
    random text new at every start. A generation is compared by equality only: a renewal the
    store replays answers ``started=False``, so a host treats a generation other than the one it
    stored as a start."""

    started: bool
    generation: str


@dataclass(frozen=True)
class LeaseStanding:
    """A member's lease as one snapshot reads it. ``holds`` is true while the lease runs and for
    one lease span after the store lost its data, when a lease may have been lost with it;
    ``generation`` is set only while the lease runs."""

    holds: bool
    generation: str | None


@dataclass(frozen=True)
class LeaseRoster:
    """The principals whose lease runs; ``settled`` once a lease span has passed since the store
    began to keep leases, so that a principal missing from ``holders`` is gone, not lost."""

    holders: frozenset[Principal]
    settled: bool


class LeaseRecord(Protocol):
    """A member's lease: kept for a fixed span after its latest renewal, on the store's clock.
    Every method raises ``PlaceStoreError`` when the store refuses or does not answer."""

    async def renew(self, principal: Principal) -> LeaseRenewal:
        """Keeps the principal's lease for another span, starting one under a new generation when
        none runs."""
        ...

    async def state(self, principal: Principal) -> LeaseStanding:
        """The principal's lease from one snapshot; it renews and starts nothing."""
        ...

    async def live(self) -> LeaseRoster:
        """Every principal whose lease runs, from one snapshot."""
        ...

    async def serve_lapses(
        self, handle: Callable[[Principal, str], Awaitable[None]], *, claimer: str
    ) -> NoReturn:
        """Hands every lapsed lease to ``handle`` with the principal and the lapsed generation, on
        one live pod, at least once, until cancelled.

        A claim on a lapse is extended while ``handle`` runs, and ``handle`` is not cancelled when
        the claim runs out; one that raises gets the lapse again once its claim has run out. Raises
        ``PlaceStoreError`` when the store refuses, after which a new call serves again.
        """
        ...


class PlaceStore(PlaceSignals, ListenRecord, Protocol):
    """What ``ResourceChanges`` takes: the signals and the listen record of one app."""
