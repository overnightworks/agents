"""``RedisPlaceStore`` on a fake Redis: the address on the wire, the error mapping and the
refusals; the two-process proof on a real Redis is in ``real_redis``."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fakeredis import FakeAsyncRedis, FakeServer

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import (
    PlaceSignal,
    PlaceStoreError,
    ResourceChange,
    Resubscribed,
    ToolsChange,
)
from agents.mcp_server.redis_place_store import RedisPlaceStore
from agents.member import STANDING_URI
from tests.support.mcp_server_support import (
    MemberApp,
    a_member,
    listen_connection,
    open_stream,
    readable_uris,
    resolve_member,
)

_PREFIX = "notes"
_ALICE = a_member("alice")


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


def a_pod(server: FakeServer, *, key_prefix: str = _PREFIX) -> RedisPlaceStore:
    return RedisPlaceStore(FakeAsyncRedis(server=server), key_prefix=key_prefix)


@asynccontextmanager
async def subscribed(store: RedisPlaceStore) -> AsyncIterator[list[PlaceSignal | Resubscribed]]:
    """The events one subscription yields, collected until the block ends."""
    events: list[PlaceSignal | Resubscribed] = []

    async def collect() -> None:
        async for event in store.received():
            events.append(event)

    task = asyncio.create_task(collect())
    while not events:
        await asyncio.sleep(0.01)
    try:
        yield events
    finally:
        task.cancel()
        await asyncio.wait({task})


async def arrived(events: list[PlaceSignal | Resubscribed], count: int) -> None:
    async with asyncio.timeout(5):
        while len(events) < count:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "signal",
    [
        ResourceChange(_ALICE, STANDING_URI, "pod-a"),
        ToolsChange(_ALICE, "pod-a"),
    ],
    ids=["a resource", "the tool list"],
)
def test_a_signal_one_pod_publishes_reaches_the_subscription_of_another(
    server: FakeServer, signal: PlaceSignal
) -> None:
    async def scenario() -> list[PlaceSignal | Resubscribed]:
        async with subscribed(a_pod(server)) as events:
            await a_pod(server).publish(signal)
            await arrived(events, 2)
            return list(events)

    assert asyncio.run(scenario()) == [Resubscribed(), signal]


def test_a_payload_that_is_no_address_is_skipped_and_logged_by_length_only(
    server: FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    channel = "notes-agents:tandem:v1:wake"
    foreign = [
        "not json at all",
        json.dumps({"kind": "resource"}),
        json.dumps(["v2", "resource", "iss", "sub", "uri", "pod"]),
        json.dumps(["v1", "resource", "iss", "sub", None, "pod"]),
    ]
    wake = ResourceChange(_ALICE, STANDING_URI, "pod-a")

    async def scenario() -> list[PlaceSignal | Resubscribed]:
        async with subscribed(a_pod(server)) as events:
            raw = FakeAsyncRedis(server=server)
            for payload in foreign:
                await raw.publish(channel, payload)
            await a_pod(server).publish(wake)
            await arrived(events, 2)
            return list(events)

    assert asyncio.run(scenario()) == [Resubscribed(), wake]
    warned = [record.getMessage() for record in caplog.records]
    assert len(warned) == len(foreign)
    assert not any("not json at all" in message for message in warned)


def test_a_failing_store_raises_place_store_error_from_publish(server: FakeServer) -> None:
    server.connected = False

    with pytest.raises(PlaceStoreError):
        asyncio.run(a_pod(server).publish(ToolsChange(_ALICE, "pod-a")))


@pytest.mark.parametrize(
    "key_prefix",
    [
        pytest.param("Not_A_Segment", id="no segment"),
        pytest.param("notes:more", id="two segments"),
        pytest.param("", id="empty"),
        pytest.param("notes-agents", id="ends in the namespace suffix"),
    ],
)
def test_a_key_prefix_that_is_no_segment_or_ends_in_agents_is_refused_naming_the_argument(
    server: FakeServer, key_prefix: str
) -> None:
    with pytest.raises(ValueError, match="key_prefix"):
        a_pod(server, key_prefix=key_prefix)


def test_a_change_on_one_front_wakes_a_listen_on_another(server: FakeServer) -> None:
    app = MemberApp(readable={_ALICE: readable_uris(STANDING_URI)})

    def a_front() -> tuple[McpPersonFront, ResourceChanges]:
        changes = ResourceChanges(a_pod(server))
        front = McpPersonFront([], resolve_person=resolve_member, resources=app, changes=changes)
        return front, changes

    front_a, _changes_a = a_front()
    front_b, changes_b = a_front()
    listening = listen_connection(front_a, _ALICE, [STANDING_URI])

    async def scenario() -> None:
        async with front_a.running(), front_b.running(), open_stream(listening):
            await changes_b.changed(_ALICE, STANDING_URI)
            async with asyncio.timeout(5):
                while not listening.updated_uris:
                    await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert listening.updated_uris == [STANDING_URI]
