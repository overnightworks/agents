"""The person front's ``subscriptions/listen``: changes per verified principal, a level trigger.

A notification only says that something changed: the client reads the resource or the tool list
afterwards, so a lost, repeated or late one leaves no stale state. An app calls
``ResourceChanges.changed`` or ``tools_changed`` only after the change is visible to a read. Both
publish an address through the place store; every pod, the publishing one included, delivers it
from its subscription to its open streams. Every open stream keeps its own pending pulses,
coalesced and in arrival order, which is why several streams of one principal each receive a change
and no overflow policy exists.

The SDK's ``ListenHandler`` checks no principal, so this module owns the handler. It registers
its stream first, so a change while the acknowledgement is being built is kept, and it checks
every pulse against ``MemberResources.current`` when the pulse is drained, queued pulses included,
so a removal ends delivery during an open stream.

Whether a principal listens lives in the place store, not in a pod: a stream that honored a URI is
recorded there with a deadline, and every pod's lapse poll turns an ended stream's answer to no.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import uuid4

import anyio
from mcp.server.context import ServerRequestContext
from mcp.shared.subscriptions import (
    SUBSCRIPTION_ID_META_KEY,
    ResourceUpdated,
    ToolsListChanged,
    event_to_notification,
)
from mcp_types import (
    ErrorData,
    ServerNotification,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsAcknowledgedNotificationParams,
    SubscriptionsListenRequestParams,
    SubscriptionsListenResult,
)

from agents.mcp_server.asgi import LISTEN_METHOD, LOGGER_NAME
from agents.mcp_server.place_store import (
    ListeningTurn,
    PlaceSignal,
    PlaceStore,
    PlaceStoreError,
    ResourceChange,
    Resubscribed,
    ToolsChange,
)
from agents.mcp_server.resources import MemberResources, failure_answer, served_uris
from agents.member import (
    LISTEN_ALLOWANCE_SECONDS,
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    place_ref_from_view_uri,
    place_ref_from_window_uri,
)
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)

LAPSE_POLL_SECONDS: Final = 1
_END_CAP_SECONDS: Final = 5
_HOLDS_OPEN: Final = timedelta(seconds=LISTEN_BOUND_SECONDS + LISTEN_ALLOWANCE_SECONDS)
_HOLDS_ENDED: Final = timedelta(seconds=LISTEN_ALLOWANCE_SECONDS)

type _Pulse = ResourceUpdated | ToolsListChanged


class _ListenStream:
    """One listen stream: its pending pulses, a repeated one staying once at its first arrival,
    and what it honors from its acknowledgement on: URIs, and whether the tool list.

    ``identity`` names it in the place store; ``recorded`` is set before the store is asked to
    record it, because a call that failed may have committed.
    """

    def __init__(self) -> None:
        self.identity = uuid4().hex
        self.recorded = False
        self._pulses: dict[_Pulse, None] = {}
        self._arrived = anyio.Event()
        self._honored: dict[str, None] = {}
        self._honors_tools = False
        self._ended = False

    def add(self, pulse: _Pulse) -> None:
        self._pulses[pulse] = None
        self._arrived.set()

    def end(self) -> None:
        self._ended = True
        self._arrived.set()

    @property
    def ended(self) -> bool:
        return self._ended

    async def next(self) -> _Pulse | None:
        """The oldest pending pulse, or ``None`` once the stream was ended."""
        while not self._pulses and not self._ended:
            await self._arrived.wait()
            self._arrived = anyio.Event()
        if self._ended:
            return None
        pulse = next(iter(self._pulses))
        del self._pulses[pulse]
        return pulse

    def resync(self) -> None:
        """Pulses everything the stream honored: each URI, and the tool list if it honored that."""
        for uri in self._honored:
            self.add(ResourceUpdated(uri))
        if self._honors_tools:
            self.add(ToolsListChanged())

    @property
    def honored(self) -> Collection[str]:
        return self._honored.keys()

    @property
    def honors_tools(self) -> bool:
        return self._honors_tools

    def honor(self, uris: Collection[str], *, tools: bool) -> None:
        self._honored = dict.fromkeys(uris)
        self._honors_tools = tools


class ResourceChanges:
    """How an app says that a member's resource or tool offer changed; no argument of a request
    reaches it.

    ``changed`` and ``tools_changed`` publish through the place store and raise
    ``PlaceStoreError`` when it fails; the change is then uncertain to have reached the other pods,
    and a member sees it at the next listen's read. The front enters ``following`` in its own
    ``running()``.

    ``listens_to`` and ``listeners`` read the place store and raise ``PlaceStoreError`` when it
    fails. ``listening_changed`` is called with the principal, the URI and the new answer whenever
    it turns: with ``True`` on the request task whose acknowledgement turned it to yes, and, on
    every pod, with the turn's direction on the signal task. The signal-task calls follow the
    store's turns in order and miss none while the subscription stands; the request-task ``True``
    is an early copy and may come before an older ``False``. A direction may repeat. A host rebuilds
    its view from ``listeners`` when ``listening_resynced`` is called, after every confirmed
    subscription, and applies the calls that arrive during the rebuild after it. Neither callable
    may block; a failure is logged by type and never reaches a listen.
    """

    def __init__(
        self,
        store: PlaceStore,
        *,
        listening_changed: Callable[[Principal, str, bool], None] | None = None,
        listening_resynced: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._origin = uuid4().hex
        self._streams: dict[Principal, set[_ListenStream]] = {}
        self._listening_changed = listening_changed
        self._listening_resynced = listening_resynced
        self._ending: set[asyncio.Task[None]] = set()
        self._dead = False

    async def changed(self, recipient: Principal, uri: str) -> None:
        """Signal every registered stream of the recipient on every pod; a stream that has ended
        and waits out its allowance never reads the pulse.

        Runs on the front's event loop or through ``anyio.from_thread.run``, and only after the
        change is visible to the read.
        """
        await self._store.publish(ResourceChange(recipient, uri, self._origin))

    async def tools_changed(self, recipient: Principal) -> None:
        """Signal every registered stream of the recipient on every pod that the tools offered to
        it changed; only a stream that honored ``toolsListChanged`` writes it.

        Runs on the front's event loop or through ``anyio.from_thread.run``, and only after the new
        offer is visible to ``offered_tools``.
        """
        await self._store.publish(ToolsChange(recipient, self._origin))

    @property
    def dead(self) -> bool:
        """Whether the subscription failed for good: no change arrives any more."""
        return self._dead

    @asynccontextmanager
    async def following(self) -> AsyncGenerator[None, None]:
        """The store's subscription as a task of its own, for as long as the block runs.

        Yields at the first ``Resubscribed`` and raises ``PlaceStoreError`` if the subscription
        fails or ends before it. A failure after it logs the cause, ends every open stream and
        turns ``dead``; the block stays entered. The task runs outside the SDK manager's task
        group, so its failure never reaches one. A second task lapses the streams of the place
        store that ended, about every ``LAPSE_POLL_SECONDS``; it runs until the block ends.
        """
        confirmed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        tasks = [asyncio.create_task(self._follow(confirmed))]
        try:
            await confirmed
            tasks.append(asyncio.create_task(self._lapse_ended_streams()))
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)

    async def _lapse_ended_streams(self) -> None:
        failing = False
        while True:
            await asyncio.sleep(LAPSE_POLL_SECONDS)
            try:
                await self._store.lapse_due(self._origin)
            except PlaceStoreError as failure:
                if not failing:
                    _LOG.warning("the place store did not lapse the ended listens: %r", failure)
                failing = True
            else:
                if failing:
                    _LOG.info("the place store lapses the ended listens again")
                failing = False

    async def _follow(self, confirmed: asyncio.Future[None]) -> None:
        try:
            async for event in self._store.received():
                match event:
                    case Resubscribed():
                        self._resync()
                        self._resynced()
                        if confirmed.done():
                            _LOG.info("the place store re-subscribed and every stream was resynced")
                        else:
                            confirmed.set_result(None)
                    case ListeningTurn(principal, uri, listens):
                        self._signal(principal, uri, listens=listens)
                    case ResourceChange() | ToolsChange():
                        self._deliver(event)
            raise PlaceStoreError("the place store's subscription ended")
        except Exception as failure:
            if confirmed.done():
                self._die(failure)
            else:
                unconfirmed = PlaceStoreError("the place store did not confirm its subscription")
                unconfirmed.__cause__ = failure
                confirmed.set_exception(unconfirmed)

    def _deliver(self, signal: PlaceSignal) -> None:
        pulse: _Pulse
        if isinstance(signal, ResourceChange):
            pulse = ResourceUpdated(signal.uri)
        else:
            pulse = ToolsListChanged()
        for stream in self._streams.get(signal.principal, ()):
            stream.add(pulse)

    def _resync(self) -> None:
        for streams in self._streams.values():
            for stream in streams:
                stream.resync()

    def _die(self, failure: Exception) -> None:
        self._dead = True
        _LOG.error("the place store's subscription failed for good: %s", failure)
        for streams in self._streams.values():
            for stream in streams:
                stream.end()

    async def listens_to(self, principal: Principal, uri: str) -> bool:
        """Whether a stream of the principal on any pod honored the URI and has not lapsed.

        A stream not yet acknowledged and one honoring only other URIs do not count; the URI
        asked is compared as it is, so a place window is not the standing, and a stream that honored
        only the tool list counts for no URI. A stream lapses ``LISTEN_ALLOWANCE_SECONDS`` after it
        ends, at the next lapse poll. Raises ``PlaceStoreError`` when the store fails.
        """
        return await self._store.listens_to(principal, uri)

    async def listeners(self, uri: str) -> frozenset[Principal]:
        """The principals for whom ``listens_to`` answers yes for the URI. Raises
        ``PlaceStoreError`` when the store fails."""
        return await self._store.listeners(uri)

    @asynccontextmanager
    async def opened(self, recipient: Principal) -> AsyncGenerator[_ListenStream, None]:
        """One stream for as long as it is open; one that was recorded in the place store is ended
        there when the block ends, cancelled or not."""
        stream = _ListenStream()
        if self._dead:
            stream.end()
        self._streams.setdefault(recipient, set()).add(stream)
        try:
            yield stream
        finally:
            self._forget(recipient, stream)
            if stream.recorded:
                await self._end_record(stream)

    async def acknowledged(
        self,
        recipient: Principal,
        stream: _ListenStream,
        honored: Collection[str],
        *,
        tools: bool,
    ) -> None:
        """The stream honors the URIs, and the tool list when ``tools``, from now on: a stream
        that honored a URI is recorded in the place store first, and the answers it turned to yes
        are called here, on the request task.

        Raises ``PlaceStoreError`` when the store fails; the stream then honors nothing and the
        record may exist, which its end lapses.
        """
        turned: frozenset[str] = frozenset()
        if honored:
            stream.recorded = True
            turned = await self._store.acknowledged(
                stream.identity, recipient, honored, _HOLDS_OPEN
            )
        stream.honor(honored, tools=tools)
        for uri in honored:
            if uri in turned:
                self._signal(recipient, uri, listens=True)

    async def _end_record(self, stream: _ListenStream) -> None:
        """Ends the stream in the store in a task of its own, so a cancellation of the listen
        never cuts it; its failure is logged by type."""
        ending = asyncio.create_task(self._end_in_store(stream))
        self._ending.add(ending)
        ending.add_done_callback(self._ending.discard)
        await asyncio.shield(ending)

    async def _end_in_store(self, stream: _ListenStream) -> None:
        try:
            async with asyncio.timeout(_END_CAP_SECONDS):
                await self._store.ended(stream.identity, _HOLDS_ENDED)
        except (PlaceStoreError, TimeoutError) as failure:
            _LOG.warning("a listen's end was not recorded: %s", type(failure).__name__)

    def _forget(self, recipient: Principal, stream: _ListenStream) -> None:
        streams = self._streams[recipient]
        streams.discard(stream)
        if not streams:
            del self._streams[recipient]

    def _signal(self, recipient: Principal, uri: str, *, listens: bool) -> None:
        if (changed := self._listening_changed) is not None:
            _call_host("listening_changed", lambda: changed(recipient, uri, listens))

    def _resynced(self) -> None:
        if (resynced := self._listening_resynced) is not None:
            _call_host("listening_resynced", resynced)


def _call_host(name: str, call: Callable[[], None]) -> None:
    try:
        call()
    except Exception as failure:
        _LOG.error("the %s callable failed: it raised %s", name, type(failure).__name__)


def _wanted(uri: str, honored: Collection[str]) -> bool:
    """Its own URI was honored, or the standing was and the URI is a place window or view."""
    return uri in honored or (
        STANDING_URI in honored
        and (place_ref_from_window_uri(uri) is not None or place_ref_from_view_uri(uri) is not None)
    )


def _honored(requested: Sequence[str], readable: Collection[str]) -> list[str]:
    """The requested URIs the principal may read, in request order and without duplicates."""
    return list(dict.fromkeys(uri for uri in requested if uri in readable))


@dataclass(frozen=True)
class _Stream:
    """One request's stream, every frame carrying the subscription id the client chose."""

    context: ServerRequestContext[Any]

    @property
    def _meta(self) -> dict[str, Any]:
        return {SUBSCRIPTION_ID_META_KEY: self.context.request_id}

    async def acknowledge(self, honored: Sequence[str], *, tools: bool) -> None:
        await self._send(
            SubscriptionsAcknowledgedNotification(
                params=SubscriptionsAcknowledgedNotificationParams(
                    notifications=SubscriptionFilter(
                        tools_list_changed=True if tools else None,
                        resource_subscriptions=list(honored) or None,
                    ),
                    _meta=self._meta,
                )
            )
        )

    async def pulse(self, pulse: _Pulse) -> None:
        await self._send(event_to_notification(pulse, self._meta))

    def completed(self) -> SubscriptionsListenResult:
        return SubscriptionsListenResult(_meta=self._meta)

    async def _send(
        self, notification: ServerNotification | SubscriptionsAcknowledgedNotification
    ) -> None:
        await self.context.session.send_notification(
            notification, related_request_id=self.context.request_id
        )


async def _deliver(
    resources: MemberResources,
    principal: Principal,
    open_stream: _ListenStream,
    stream: _Stream,
) -> None:
    """Writes each wanted pulse, a resource's only while the principal may still read it, until a
    port failure or the end of the stream ends it."""
    while (pulse := await open_stream.next()) is not None:
        if isinstance(pulse, ToolsListChanged):
            if open_stream.honors_tools:
                await stream.pulse(pulse)
            continue
        if not _wanted(pulse.uri, open_stream.honored):
            continue
        try:
            still_readable = pulse.uri in await served_uris(resources, principal)
        except Exception as failure:
            failure_answer(LISTEN_METHOD, failure)
            return
        if still_readable:
            await stream.pulse(pulse)


async def on_subscriptions_listen(
    resources: MemberResources,
    changes: ResourceChanges,
    principal: Principal,
    context: ServerRequestContext[Any],
    params: SubscriptionsListenRequestParams,
    *,
    announces_tools: bool,
) -> SubscriptionsListenResult | ErrorData:
    """One stream for the verified principal; it ends at ``LISTEN_BOUND_SECONDS``.

    ``toolsListChanged`` is honored when asked and ``announces_tools``; no other list-changed
    flag is. A stream that honored a URI is recorded in the place store before its
    acknowledgement is written, and a store that fails answers like a port that fails. No
    exception of the port reaches the SDK; a cancellation propagates.
    """
    stream = _Stream(context)
    async with changes.opened(principal) as open_stream:
        with anyio.move_on_after(LISTEN_BOUND_SECONDS):
            if open_stream.ended:
                return stream.completed()
            try:
                readable = await served_uris(resources, principal)
            except Exception as failure:
                return failure_answer(LISTEN_METHOD, failure)
            honored = _honored(params.notifications.resource_subscriptions or (), readable)
            tools = announces_tools and params.notifications.tools_list_changed is True
            try:
                await changes.acknowledged(principal, open_stream, honored, tools=tools)
            except PlaceStoreError as failure:
                return failure_answer(LISTEN_METHOD, failure)
            await stream.acknowledge(honored, tools=tools)
            await _deliver(resources, principal, open_stream, stream)
    return stream.completed()
