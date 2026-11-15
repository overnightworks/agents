"""A change on one pod wakes the open streams of every pod through the place store.

The store is the in-memory ``PlaceSignalsFake``: it shows the front's reaction to the subscription,
its confirmation, its re-confirmation and its death. The Redis adapter has its own suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import PlaceStoreError, ResourceChange
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.mcp_server_support import (
    MemberApp,
    ToolOffer,
    listen_connection,
    open_stream,
    post,
    resolve_member,
    rpc_body,
    settled,
    window_uri,
)
from tests.support.place_store_support import PlaceSignalsFake

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
place_signals = listen_support.place_signals
front = listen_support.front
served = listen_support.served

_ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
_UPDATED = "notifications/resources/updated"
_TOOLS_CHANGED = "notifications/tools/list_changed"
_ANOTHER_POD = "another-pod"


def failing_with(place_signals: PlaceSignalsFake, how: str) -> Callable[[], None]:
    """The store's subscription raising, or ending, once what came before it was read."""
    if how == "raises":
        return lambda: place_signals.fail(PlaceStoreError("the subscription was refused"))
    return place_signals.end


def frames_of(connection: Any) -> list[tuple[str | None, str | None]]:
    return [
        (frame.get("method"), frame.get("params", {}).get("uri")) for frame in connection.frames
    ]


def test_a_change_publishes_an_address_that_names_the_member_and_the_uri_and_no_content(
    changes: ResourceChanges, place_signals: PlaceSignalsFake, alice: Principal
) -> None:
    served(changes.changed(alice, STANDING_URI))

    [published] = place_signals.published
    assert isinstance(published, ResourceChange)
    assert (published.principal, published.uri) == (alice, STANDING_URI)


def test_a_failing_publish_raises_place_store_error_and_wakes_no_stream(
    front: McpPersonFront,
    changes: ResourceChanges,
    place_signals: PlaceSignalsFake,
    alice: Principal,
) -> None:
    place_signals.publish_failure = PlaceStoreError("the store did not answer")
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            with pytest.raises(PlaceStoreError):
                await changes.changed(alice, STANDING_URI)
            await settled()

    served(scenario())

    assert connection.updated_uris == []


def test_a_signal_another_pod_published_reaches_the_streams_of_its_member_only(
    front: McpPersonFront, place_signals: PlaceSignalsFake, alice: Principal, bob: Principal
) -> None:
    streams = {
        "alice": listen_connection(front, alice, [STANDING_URI]),
        "bob": listen_connection(front, bob, [STANDING_URI]),
    }

    async def scenario() -> None:
        async with front.running(), open_stream(streams["alice"]), open_stream(streams["bob"]):
            place_signals.deliver(ResourceChange(alice, STANDING_URI, _ANOTHER_POD))
            await settled()

    served(scenario())

    assert {name: stream.updated_uris for name, stream in streams.items()} == {
        "alice": [STANDING_URI],
        "bob": [],
    }


def test_running_yields_only_once_the_first_subscription_is_confirmed(
    front: McpPersonFront, place_signals: PlaceSignalsFake
) -> None:
    place_signals.confirms_subscription = False

    async def scenario() -> list[bool]:
        entered = asyncio.Event()
        leave = asyncio.Event()

        async def serve() -> None:
            async with front.running():
                entered.set()
                await leave.wait()

        serving = asyncio.create_task(serve())
        await settled()
        before = entered.is_set()
        place_signals.resubscribe()
        await settled()
        after = entered.is_set()
        leave.set()
        await serving
        return [before, after]

    assert served(scenario()) == [False, True]


@pytest.mark.parametrize("how", ["raises", "ends"])
def test_running_raises_place_store_error_when_the_subscription_fails_before_it_is_confirmed(
    front: McpPersonFront, place_signals: PlaceSignalsFake, how: str
) -> None:
    place_signals.confirms_subscription = False
    failing_with(place_signals, how)()

    async def scenario() -> None:
        async with front.running():
            pytest.fail("the front served without a confirmed subscription")

    with pytest.raises(PlaceStoreError):
        served(scenario())


def test_a_later_subscription_gives_each_stream_what_it_honored_and_logs_one_line_each(
    app: MemberApp,
    alice: Principal,
    place_signals: PlaceSignalsFake,
    changes: ResourceChanges,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    offer = ToolOffer(offered={alice: ()})
    front = McpPersonFront(
        [],
        resolve_person=resolve_member,
        resources=app,
        changes=changes,
        offered_tools=offer,
    )
    first = window_uri("first")
    watching_everything = listen_connection(
        front, alice, [STANDING_URI, first], flags={"toolsListChanged": True}
    )
    watching_the_standing = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> int:
        async with front.running(), open_stream(watching_everything):
            async with open_stream(watching_the_standing):
                logged_before = len(caplog.records)
                place_signals.resubscribe()
                await settled()
                place_signals.resubscribe()
                await settled()
        return len(caplog.records) - logged_before

    logged = served(scenario())

    assert logged == 2
    assert sorted(
        (method or "", uri or "")
        for method, uri in frames_of(watching_everything)
        if method != _ACKNOWLEDGED
    ) == sorted([(_UPDATED, STANDING_URI), (_UPDATED, first), (_TOOLS_CHANGED, "")] * 2)
    assert watching_the_standing.updated_uris == [STANDING_URI] * 2


@pytest.mark.parametrize("how", ["raises", "ends"])
def test_a_subscription_that_fails_later_ends_the_open_listens_and_answers_503_before_resolving(
    app: MemberApp,
    alice: Principal,
    place_signals: PlaceSignalsFake,
    changes: ResourceChanges,
    caplog: pytest.LogCaptureFixture,
    how: str,
) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    resolved: list[Principal] = []

    def counting_resolver(scope: dict[str, Any]) -> Principal:
        resolved.append(resolve_member(scope))
        return resolved[-1]

    front = McpPersonFront([], resolve_person=counting_resolver, resources=app, changes=changes)
    listening = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> list[int]:
        async with front.running(), open_stream(listening):
            resolved_before = len(resolved)
            failing_with(place_signals, how)()
            await settled()
            statuses = [
                (await post(front, alice, rpc_body("tools/list")))[0]["status"] for _ in range(2)
            ]
            return [*statuses, len(resolved) - resolved_before]

    *statuses, resolved_after = served(scenario())

    assert statuses == [HTTPStatus.SERVICE_UNAVAILABLE] * 2
    assert resolved_after == 0
    assert listening.returned_after is not None
    assert "result" in listening.frames[-1]
    assert len([record for record in caplog.records if record.levelno >= logging.ERROR]) == 1


def test_a_subscription_failing_for_good_logs_its_cause(
    front: McpPersonFront,
    place_signals: PlaceSignalsFake,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    async def scenario() -> None:
        async with front.running():
            place_signals.fail(PlaceStoreError("no permission for the channel"))
            await settled()

    served(scenario())

    assert "no permission for the channel" in " ".join(
        record.getMessage() for record in caplog.records
    )


def test_a_listen_that_passed_the_dead_check_before_the_subscription_failed_opens_no_stream(
    front: McpPersonFront, app: MemberApp, place_signals: PlaceSignalsFake, alice: Principal
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])
    request_released = asyncio.Event()
    delivered_request = connection.receive

    async def receive_when_released() -> dict[str, Any]:
        await request_released.wait()
        return await delivered_request()

    connection.receive = receive_when_released

    async def scenario() -> None:
        async with front.running():
            serving = asyncio.create_task(connection.run())
            await settled()
            place_signals.fail(PlaceStoreError("the subscription was refused"))
            await settled()
            request_released.set()
            await serving

    served(scenario())

    assert json.loads(connection.body_bytes)["result"]["resultType"] == "complete"
    assert app.current_asked == []
