"""A change on one pod wakes the open streams of every pod through the place store.

The store is the in-memory ``PlaceStoreFake``: it shows the front's reaction to the subscription,
its confirmation, its re-confirmation and its death. The Redis adapter has its own suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import PlaceStoreError, ResourceChange
from agents.member import STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.listen_support import ListeningHost
from tests.support.log_lines import LogLines, log_lines
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
from tests.support.place_store_support import PlaceStoreFake, refused_with_a_cause

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
place_store = listen_support.place_store
front = listen_support.front
served = listen_support.served
log_lines = log_lines

_ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
_UPDATED = "notifications/resources/updated"
_TOOLS_CHANGED = "notifications/tools/list_changed"
_ANOTHER_POD = "another-pod"


def failing_with(place_store: PlaceStoreFake, how: str) -> Callable[[], None]:
    """The store's subscription raising, or ending, once what came before it was read."""
    if how == "raises":
        return lambda: place_store.fail(PlaceStoreError("the subscription was refused"))
    return place_store.end


def frames_of(connection: Any) -> list[tuple[str | None, str | None]]:
    return [
        (frame.get("method"), frame.get("params", {}).get("uri")) for frame in connection.frames
    ]


def test_a_change_publishes_an_address_that_names_the_member_and_the_uri_and_no_content(
    changes: ResourceChanges, place_store: PlaceStoreFake, alice: Principal
) -> None:
    served(changes.changed(alice, STANDING_URI))

    [published] = place_store.published
    assert isinstance(published, ResourceChange)
    assert (published.principal, published.uri) == (alice, STANDING_URI)


def test_a_failing_publish_raises_place_store_error_and_wakes_no_stream(
    front: McpPersonFront,
    changes: ResourceChanges,
    place_store: PlaceStoreFake,
    alice: Principal,
    log_lines: LogLines,
) -> None:
    place_store.publish_failure = refused_with_a_cause()
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            with pytest.raises(PlaceStoreError):
                await changes.changed(alice, STANDING_URI)
            await settled()

    served(scenario())

    assert connection.updated_uris == []
    failed = log_lines.one("place_publish.ended")
    assert (
        failed["outcome"],
        failed["signal_kind"],
        failed["error_type"],
        failed["cause_type"],
    ) == ("failed", "resource", "PlaceStoreError", "PermissionError")


@pytest.mark.parametrize(
    ("publish", "signal_kind"),
    [
        pytest.param(
            lambda changes, principal: changes.changed(principal, STANDING_URI),
            "resource",
            id="a resource change",
        ),
        pytest.param(
            lambda changes, principal: changes.tools_changed(principal),
            "tools",
            id="a tool list change",
        ),
    ],
)
def test_a_publish_is_one_ok_line_with_its_kind_and_no_address(
    changes: ResourceChanges,
    alice: Principal,
    log_lines: LogLines,
    publish: Callable[[ResourceChanges, Principal], Awaitable[None]],
    signal_kind: str,
) -> None:
    served(publish(changes, alice))

    published = log_lines.one("place_publish.ended")
    assert (published["outcome"], published["signal_kind"]) == ("ok", signal_kind)
    assert STANDING_URI not in log_lines.everything()


def test_a_signal_another_pod_published_reaches_the_streams_of_its_member_only(
    front: McpPersonFront, place_store: PlaceStoreFake, alice: Principal, bob: Principal
) -> None:
    streams = {
        "alice": listen_connection(front, alice, [STANDING_URI]),
        "bob": listen_connection(front, bob, [STANDING_URI]),
    }

    async def scenario() -> None:
        async with front.running(), open_stream(streams["alice"]), open_stream(streams["bob"]):
            place_store.deliver(ResourceChange(alice, STANDING_URI, _ANOTHER_POD))
            await settled()

    served(scenario())

    assert {name: stream.updated_uris for name, stream in streams.items()} == {
        "alice": [STANDING_URI],
        "bob": [],
    }


def test_running_yields_only_once_the_first_subscription_is_confirmed(
    front: McpPersonFront, place_store: PlaceStoreFake
) -> None:
    place_store.confirms_subscription = False

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
        place_store.resubscribe()
        await settled()
        after = entered.is_set()
        leave.set()
        await serving
        return [before, after]

    assert served(scenario()) == [False, True]


def test_listening_resynced_is_called_at_every_confirmed_subscription_the_first_included(
    place_store: PlaceStoreFake, app: MemberApp
) -> None:
    host = ListeningHost(place_store)
    front = McpPersonFront([], resolve_person=resolve_member, resources=app, changes=host.changes())

    async def scenario() -> list[int]:
        counts = []
        async with front.running():
            counts.append(host.resyncs)
            place_store.resubscribe()
            await settled()
            counts.append(host.resyncs)
        return counts

    assert served(scenario()) == [1, 2]


@pytest.mark.parametrize("how", ["raises", "ends"])
def test_running_raises_place_store_error_when_the_subscription_fails_before_it_is_confirmed(
    front: McpPersonFront, place_store: PlaceStoreFake, how: str
) -> None:
    place_store.confirms_subscription = False
    failing_with(place_store, how)()

    async def scenario() -> None:
        async with front.running():
            pytest.fail("the front served without a confirmed subscription")

    serving = scenario()

    with pytest.raises(PlaceStoreError):
        served(serving)


def test_a_later_subscription_resyncs_each_stream_and_writes_one_renewed_line_each_time(
    app: MemberApp,
    alice: Principal,
    place_store: PlaceStoreFake,
    changes: ResourceChanges,
    log_lines: LogLines,
) -> None:
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

    async def scenario() -> None:
        async with front.running(), open_stream(watching_everything):
            async with open_stream(watching_the_standing):
                place_store.resubscribe()
                await settled()
                place_store.resubscribe()
                await settled()

    served(scenario())

    assert [
        (line["level"], line["stream_count"]) for line in log_lines.of("place_subscription.renewed")
    ] == [("info", 2)] * 2
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
    place_store: PlaceStoreFake,
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
            failing_with(place_store, how)()
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


def test_a_subscription_failing_for_good_writes_one_error_line_with_the_type_of_its_cause(
    front: McpPersonFront, place_store: PlaceStoreFake, log_lines: LogLines
) -> None:
    refusal = PlaceStoreError("no permission for the channel")
    refusal.__cause__ = PermissionError("the secret detail")

    async def scenario() -> None:
        async with front.running():
            place_store.fail(refusal)
            await settled()

    served(scenario())

    failed = log_lines.one("place_subscription.failed")
    assert (failed["level"], failed["error_type"]) == ("error", "PermissionError")
    assert "secret detail" not in log_lines.everything()
    assert "no permission for the channel" not in log_lines.everything()


def test_a_listen_that_passed_the_dead_check_before_the_subscription_failed_opens_no_stream(
    front: McpPersonFront, app: MemberApp, place_store: PlaceStoreFake, alice: Principal
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
            place_store.fail(PlaceStoreError("the subscription was refused"))
            await settled()
            request_released.set()
            await serving

    served(scenario())

    assert json.loads(connection.body_bytes)["result"]["resultType"] == "complete"
    assert app.current_asked == []
