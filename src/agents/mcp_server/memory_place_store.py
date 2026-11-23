"""The place store in memory, for a host's tests.

``InMemoryPlaceStore`` is what every pod of an app shares: a ``ResourceChanges`` of each pod takes
the same instance, as the pods of a Redis app take the same server. Time is the running loop's, so
a virtual clock moves a stream's deadline. It imports no store library.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Collection
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, NoReturn

from agents.mcp_server.place_store import (
    ListeningTurn,
    PlaceSignal,
    PlaceStoreError,
)
from agents.turn import Principal

type _Event = PlaceSignal | ListeningTurn


class _Resync:
    """The store confirmed the subscription, the first time or after a reconnect."""


_RESYNC: Final = _Resync()

type _Inbound = _Event | _Resync | Exception


@dataclass
class _StreamRecord:
    principal: Principal
    uris: frozenset[str]
    due: float


@dataclass
class _Follower:
    on_resync: Callable[[], None]
    on_signal: Callable[[_Event], None]
    on_failed: Callable[[Exception], None]
    inbox: asyncio.Queue[_Inbound] = field(default_factory=asyncio.Queue[_Inbound])


class InMemoryPlaceStore:
    """A ``PlaceStore`` whose signals, listen record and subscriptions live in this object.

    A subscription confirms as soon as it is entered, unless ``confirms_subscription`` is false:
    then none confirms until ``resubscribe``. A signal published before a subscription's
    confirmation is never handed to it, as a store drops what it sent before the subscription.
    ``resubscribe`` confirms every subscription again, as after a reconnect, and
    ``fail_subscriptions`` ends them for good; both let a test drive what a store does that no call
    asks for. ``hold_signals`` keeps every subscription from handling signals and turns until
    ``release_signals``, as a slow network does.

    The port's calls are async and this store waits for nothing in most of them, so those carry
    ``NOSONAR(S7503)``.
    """

    def __init__(self, *, confirms_subscription: bool = True) -> None:
        self._confirms_subscription = confirms_subscription
        self._streams: dict[str, _StreamRecord] = {}
        self._followers: list[_Follower] = []
        self._released = asyncio.Event()
        self._released.set()

    async def publish(self, signal: PlaceSignal) -> None:  # NOSONAR(S7503)
        self._broadcast(signal)

    @asynccontextmanager
    async def following(
        self,
        *,
        on_resync: Callable[[], None],
        on_signal: Callable[[PlaceSignal | ListeningTurn], None],
        on_failed: Callable[[Exception], None],
        ready_within: timedelta,
    ) -> AsyncGenerator[None]:
        if ready_within <= timedelta(0):
            raise ValueError("ready_within must be positive")
        follower = _Follower(on_resync, on_signal, on_failed)
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entered = asyncio.Event()
        self._followers.append(follower)
        if self._confirms_subscription:
            follower.inbox.put_nowait(_RESYNC)
        following = asyncio.create_task(self._follow(follower, ready, entered))
        try:
            deadline = asyncio.timeout(ready_within.total_seconds())
            try:
                async with deadline:
                    await ready
            except TimeoutError as unconfirmed:
                if not deadline.expired():
                    raise
                raise PlaceStoreError(
                    f"the subscription was not confirmed within {ready_within}"
                ) from unconfirmed
            entered.set()
            yield
        finally:
            following.cancel()
            self._followers.remove(follower)
            with suppress(asyncio.CancelledError):
                await following

    async def acknowledged(  # NOSONAR(S7503)
        self, stream: str, principal: Principal, uris: Collection[str], holds_for: timedelta
    ) -> frozenset[str]:
        if stream in self._streams:
            return frozenset()
        turned = frozenset(uri for uri in uris if principal not in self._listeners(uri))
        due = asyncio.get_running_loop().time() + holds_for.total_seconds()
        self._streams[stream] = _StreamRecord(principal, frozenset(uris), due)
        for uri in sorted(turned):
            self._broadcast(ListeningTurn(principal, uri, listens=True))
        return turned

    async def ended(self, stream: str, holds_for: timedelta) -> None:  # NOSONAR(S7503)
        record = self._streams.get(stream)
        shortened = asyncio.get_running_loop().time() + holds_for.total_seconds()
        if record is not None and record.due > shortened:
            record.due = shortened

    async def listens_to(self, principal: Principal, uri: str) -> bool:  # NOSONAR(S7503)
        return principal in self._listeners(uri)

    async def listeners(self, uri: str) -> frozenset[Principal]:  # NOSONAR(S7503)
        return self._listeners(uri)

    async def serve_stream_lapses(
        self, lapsed: Callable[[ListeningTurn], None], *, every: timedelta
    ) -> NoReturn:
        while True:
            await asyncio.sleep(every.total_seconds())
            for turn in self._lapse_due():
                lapsed(turn)

    def resubscribe(self) -> None:
        for follower in self._followers:
            follower.inbox.put_nowait(_RESYNC)

    def fail_subscriptions(self, failure: Exception) -> None:
        for follower in self._followers:
            follower.inbox.put_nowait(failure)

    def hold_signals(self) -> None:
        self._released.clear()

    def release_signals(self) -> None:
        self._released.set()

    def _lapse_due(self) -> list[ListeningTurn]:
        now = asyncio.get_running_loop().time()
        turns: list[ListeningTurn] = []
        for stream in [name for name, record in self._streams.items() if record.due <= now]:
            record = self._streams.pop(stream)
            for uri in sorted(record.uris):
                if record.principal not in self._listeners(uri):
                    turns.append(ListeningTurn(record.principal, uri, listens=False))
                    self._broadcast(turns[-1])
        return turns

    def _listeners(self, uri: str) -> frozenset[Principal]:
        return frozenset(r.principal for r in self._streams.values() if uri in r.uris)

    def _broadcast(self, event: _Event) -> None:
        for follower in self._followers:
            follower.inbox.put_nowait(event)

    async def _follow(
        self, follower: _Follower, ready: asyncio.Future[None], entered: asyncio.Event
    ) -> None:
        confirmed = False
        try:
            while True:
                event = await follower.inbox.get()
                if isinstance(event, Exception):
                    raise event
                if isinstance(event, _Resync):
                    confirmed = True
                    follower.on_resync()
                    if not ready.done():
                        ready.set_result(None)
                        await entered.wait()
                elif confirmed:
                    await self._released.wait()
                    follower.on_signal(event)
        except Exception as failure:
            if ready.done():
                follower.on_failed(failure)
            else:
                ready.set_exception(failure)
