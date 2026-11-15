"""The ports to the store that every pod of an app shares; no module here imports a store library.

``PlaceSignals`` carries an address, never content: a signal names the member and the URI or the
tool list that changed, and the pod that receives it reads the app's own truth. A host that runs
more than one pod gives ``ResourceChanges`` the implementation in
``agents.mcp_server.redis_place_store``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
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

    def received(self) -> AsyncIterator[PlaceSignal | Resubscribed]:
        """The confirmation of every subscription and each signal after it, until cancelled.

        Raises ``PlaceStoreError`` when the subscription cannot recover.
        """
        ...
