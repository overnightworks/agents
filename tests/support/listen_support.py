"""The arrangement the listen suites share: two members, their app, the changes and the front."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support.mcp_server_support import (
    ListenConnection,
    MemberApp,
    a_member,
    do_nothing,
    open_stream,
    readable_uris,
    resolve_member,
    settled,
    window_uri,
)
from tests.support.place_store_support import PlaceStoreFake
from tests.support.virtual_time import in_virtual_time


@pytest.fixture
def alice() -> Principal:
    return a_member("alice")


@pytest.fixture
def bob() -> Principal:
    return a_member("bob")


@pytest.fixture
def app(alice: Principal, bob: Principal) -> MemberApp:
    return MemberApp(
        readable={
            alice: readable_uris(STANDING_URI, window_uri("first"), window_uri("second")),
            bob: readable_uris(STANDING_URI),
        }
    )


@pytest.fixture
def place_store() -> PlaceStoreFake:
    return PlaceStoreFake()


@pytest.fixture
def changes(place_store: PlaceStoreFake) -> ResourceChanges:
    return ResourceChanges(place_store)


@pytest.fixture
def front(app: MemberApp, changes: ResourceChanges) -> McpPersonFront:
    return McpPersonFront([], resolve_person=resolve_member, resources=app, changes=changes)


type Task = Literal["request", "signal"]


@dataclass
class ListeningHost:
    """A host's callables for one pod: every call with the task it came on, the standing a host
    that follows the signal-task calls holds, and the resyncs.

    ``on_call`` runs after each call, so a test can act between two of them.
    """

    store: PlaceStoreFake
    calls: list[tuple[Principal, str, bool, Task]] = field(default_factory=list)
    followed: dict[tuple[Principal, str], bool] = field(default_factory=dict)
    resyncs: int = 0
    on_call: Callable[[Task], None] = lambda _task: None

    def changed(self, principal: Principal, uri: str, listens: bool) -> None:
        task: Task = "signal" if asyncio.current_task() is self.store.signal_task else "request"
        self.calls.append((principal, uri, listens, task))
        if task == "signal":
            self.followed[principal, uri] = listens
        self.on_call(task)

    def resynced(self) -> None:
        self.resyncs += 1

    def changes(self) -> ResourceChanges:
        return ResourceChanges(
            self.store, listening_changed=self.changed, listening_resynced=self.resynced
        )


async def signalled_around_acknowledgement(
    front: McpPersonFront,
    app: MemberApp,
    connection: ListenConnection,
    signal: Callable[[], Awaitable[None]],
    *,
    while_the_port_answers: bool,
) -> None:
    """Opens the stream and signals once, while the port answers the acknowledgement or after it."""

    async def signal_once() -> None:
        app.before_current = do_nothing
        await signal()

    if while_the_port_answers:
        app.before_current = signal_once
    async with front.running(), open_stream(connection):
        if not while_the_port_answers:
            await signal_once()
        await settled()


def served[T](main: Coroutine[Any, Any, T]) -> T:
    return in_virtual_time(main)
