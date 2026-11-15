"""The ports to the store that every pod of an app shares; no module here imports a store library.

``PlaceSignals`` carries an address, never content: a wake names the member and the URI or the
tool list that changed, and the pod that receives it reads the app's own truth. A listening turn
carries its direction as well. ``ListenRecord`` keeps which principal listens to which URI, on
every pod alike. A host that runs more than one pod gives ``ResourceChanges`` the implementation in
``agents.mcp_server.redis_place_store``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from agents.turn import Principal


class PlaceStoreError(Exception):
    """The store refused or did not answer; whether a publish arrived is uncertain."""


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


class PlaceStore(PlaceSignals, ListenRecord, Protocol):
    """What ``ResourceChanges`` takes: the signals and the listen record of one app."""
