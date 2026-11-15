"""An in-memory ``PlaceSignals`` for the suites that run ``ResourceChanges`` without a store."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from agents.mcp_server.place_store import PlaceSignal, ResourceChange, Resubscribed, ToolsChange

_ENDED = object()


@dataclass
class PlaceSignalsFake:
    """One pod's view of the store: a published signal comes back through ``received``, as it does
    from a real subscription of the publishing pod.

    ``received`` opens with a ``Resubscribed`` unless ``confirms_subscription`` is false, which a
    test then sends with ``resubscribe``. ``fail`` and ``end`` make ``received`` raise or finish
    once the events before them were read; ``publish_failure`` makes every publish raise.
    """

    confirms_subscription: bool = True
    publish_failure: Exception | None = None
    published: list[PlaceSignal] = field(default_factory=list)
    received_calls: int = 0
    _events: asyncio.Queue[PlaceSignal | Resubscribed | Exception | object] = field(
        default_factory=asyncio.Queue
    )

    async def publish(self, signal: PlaceSignal) -> None:
        if self.publish_failure is not None:
            raise self.publish_failure
        self.published.append(signal)
        self._events.put_nowait(signal)

    async def received(self) -> AsyncIterator[PlaceSignal | Resubscribed]:
        self.received_calls += 1
        if self.confirms_subscription:
            yield Resubscribed()
        while True:
            event = await self._events.get()
            if event is _ENDED:
                return
            if isinstance(event, Exception):
                raise event
            assert isinstance(event, ResourceChange | ToolsChange | Resubscribed)
            yield event

    def resubscribe(self) -> None:
        self._events.put_nowait(Resubscribed())

    def deliver(self, signal: PlaceSignal) -> None:
        """A signal another pod published."""
        self._events.put_nowait(signal)

    def fail(self, failure: Exception) -> None:
        self._events.put_nowait(failure)

    def end(self) -> None:
        self._events.put_nowait(_ENDED)
