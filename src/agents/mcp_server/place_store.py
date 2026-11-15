"""The ports to the store that every pod of an app shares; no module here imports a store library.

``PlaceSignals`` carries an address, never content: a wake names the member and the URI or the
tool list that changed, and the pod that receives it reads the app's own truth. A listening turn
carries its direction as well. ``ListenRecord`` keeps which principal listens to which URI, on
every pod alike. ``LeaseRecord`` keeps a member's lease, which survives the pod that renewed it. A
host that runs more than one pod gives ``ResourceChanges`` and ``MemberLeases`` the implementation
in ``agents.mcp_server.redis_place_store``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn, Protocol

from agents.turn import Principal


class PlaceStoreError(Exception):
    """The store refused or did not answer; whether a publish arrived is uncertain."""


def failure_type(failure: Exception) -> str:
    """The type name a log line carries for a failure: the cause of a ``PlaceStoreError``, which
    is the store library's refusal, else the failure's own type; never its text."""
    cause = failure.__cause__
    if isinstance(failure, PlaceStoreError) and cause is not None:
        return type(cause).__name__
    return type(failure).__name__


@dataclass(frozen=True)
class ResourceChange:
    """The resource at ``uri`` of ``principal`` changed; ``origin`` names the publishing pod."""

    principal: Principal
    uri: str
    origin: str


@dataclass(frozen=True)
class ToolsChange:
    """The tools offered to ``principal`` changed; ``origin`` names the publishing pod."""

    principal: Principal
    origin: str


type PlaceSignal = ResourceChange | ToolsChange


@dataclass(frozen=True)
class ListeningTurn:
    """The answer to whether ``principal`` listens to ``uri`` turned to ``listens``; only the
    record publishes one."""

    principal: Principal
    uri: str
    listens: bool


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

    async def lapse_due(self, claimer: str) -> None:
        """Lapses every stream whose deadline passed that nobody else lapses now; a principal's
        last stream of a URI turns it to no and publishes a ``ListeningTurn``."""
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
