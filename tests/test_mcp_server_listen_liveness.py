"""Whether a principal listens to a URI: the answer ``ResourceChanges.listens_to`` gives an app,
and the calls ``listening_changed`` makes on every pod."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.listen import LAPSE_POLL_SECONDS
from agents.mcp_server.place_store import PlaceStoreError
from agents.member import LISTEN_ALLOWANCE_SECONDS, LISTEN_BOUND_SECONDS, STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.listen_support import ListeningHost
from tests.support.mcp_server_support import (
    MemberApp,
    listen_connection,
    open_stream,
    resolve_member,
    settled,
    window_uri,
)
from tests.support.place_store_support import PlaceStoreFake

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
front = listen_support.front
served = listen_support.served
place_store = listen_support.place_store

_PAST_THE_ALLOWANCE = LISTEN_ALLOWANCE_SECONDS + 2 * LAPSE_POLL_SECONDS
_FAILED_TEXT = "the resource failed"


@pytest.fixture
def host(place_store: PlaceStoreFake) -> ListeningHost:
    return ListeningHost(place_store)


@pytest.fixture
def signalling_changes(host: ListeningHost) -> ResourceChanges:
    return host.changes()


@pytest.fixture
def signalling_front(app: MemberApp, signalling_changes: ResourceChanges) -> McpPersonFront:
    return McpPersonFront(
        [], resolve_person=resolve_member, resources=app, changes=signalling_changes
    )


def a_front(app: MemberApp, pod: PlaceStoreFake) -> tuple[McpPersonFront, ListeningHost]:
    """Another pod of the app on the same store, with its own host."""
    pod_host = ListeningHost(pod)
    front = McpPersonFront(
        [], resolve_person=resolve_member, resources=app, changes=pod_host.changes()
    )
    return front, pod_host


@pytest.mark.parametrize(
    ("requested", "asked", "listens"),
    [
        pytest.param([STANDING_URI], STANDING_URI, True, id="the standing is acknowledged"),
        pytest.param(
            [window_uri("first"), STANDING_URI],
            STANDING_URI,
            True,
            id="the standing among other places",
        ),
        pytest.param([window_uri("first")], STANDING_URI, False, id="only a place window"),
        pytest.param(
            [window_uri("foreign")],
            STANDING_URI,
            False,
            id="a requested URI the principal may not read",
        ),
        pytest.param([], STANDING_URI, False, id="nothing requested"),
        pytest.param(
            [STANDING_URI],
            window_uri("first"),
            False,
            id="a place window while only the standing is acknowledged",
        ),
    ],
)
def test_a_principal_listens_to_a_uri_when_an_acknowledged_stream_honored_it(
    front: McpPersonFront,
    alice: Principal,
    changes: ResourceChanges,
    requested: list[str],
    asked: str,
    listens: bool,
) -> None:
    connection = listen_connection(front, alice, requested)

    async def scenario() -> bool:
        async with front.running(), open_stream(connection):
            return await changes.listens_to(alice, asked)

    assert served(scenario()) is listens


def test_another_member_listening_to_the_standing_does_not_make_a_principal_listen(
    front: McpPersonFront, alice: Principal, bob: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, bob, [STANDING_URI])

    async def scenario() -> bool:
        async with front.running(), open_stream(connection):
            return await changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) is False


def test_the_listeners_of_a_uri_are_the_principals_with_an_acknowledged_stream_honoring_it(
    front: McpPersonFront, alice: Principal, bob: Principal, changes: ResourceChanges
) -> None:
    streams = [
        listen_connection(front, alice, [STANDING_URI], request_id=1),
        listen_connection(front, bob, [STANDING_URI], request_id=2),
    ]

    async def scenario() -> tuple[frozenset[Principal], frozenset[Principal]]:
        async with front.running(), open_stream(streams[0]), open_stream(streams[1]):
            return (
                await changes.listeners(STANDING_URI),
                await changes.listeners(window_uri("first")),
            )

    assert served(scenario()) == (frozenset({alice, bob}), frozenset())


def test_a_stream_covers_the_standing_only_once_it_is_acknowledged(
    front: McpPersonFront, app: MemberApp, alice: Principal, changes: ResourceChanges
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])
    app.current_seconds = 5

    async def scenario() -> tuple[bool, bool]:
        async with front.running(), open_stream(connection):
            before = await changes.listens_to(alice, STANDING_URI)
            await asyncio.sleep(5)
            return before, await changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) == (False, True)


@pytest.mark.parametrize(
    "staying_seconds",
    [
        pytest.param(0, id="the client leaves"),
        pytest.param(LISTEN_BOUND_SECONDS * 2, id="the bound"),
    ],
)
def test_an_ended_stream_no_longer_covers_the_standing_once_the_allowance_has_passed(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges, staying_seconds: float
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> bool:
        async with front.running():
            async with open_stream(connection):
                await asyncio.sleep(staying_seconds)
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            return await changes.listens_to(alice, STANDING_URI)

    assert served(scenario()) is False


def test_several_streams_of_one_principal_cover_the_standing_while_any_of_them_does(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges
) -> None:
    place_only = listen_connection(front, alice, [window_uri("first")])
    standing = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        answers = []
        async with front.running(), open_stream(place_only):
            async with open_stream(standing):
                answers.append(await changes.listens_to(alice, STANDING_URI))
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            answers.append(await changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, False]


def test_the_acknowledging_pod_calls_yes_on_its_request_task_and_again_on_its_signal_task(
    signalling_front: McpPersonFront, host: ListeningHost, alice: Principal
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running(), open_stream(connection):
            pass

    served(scenario())

    assert host.calls == [
        (alice, STANDING_URI, True, "request"),
        (alice, STANDING_URI, True, "signal"),
    ]


def test_another_pod_calls_yes_on_its_signal_task_and_no_once_the_allowance_has_passed(
    signalling_front: McpPersonFront,
    host: ListeningHost,
    place_store: PlaceStoreFake,
    app: MemberApp,
    alice: Principal,
) -> None:
    front_b, host_b = a_front(app, place_store.shared.pod())
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running(), front_b.running():
            async with open_stream(connection):
                pass
            await asyncio.sleep(_PAST_THE_ALLOWANCE)

    served(scenario())

    assert host_b.calls == [
        (alice, STANDING_URI, True, "signal"),
        (alice, STANDING_URI, False, "signal"),
    ]
    assert host.calls == [
        (alice, STANDING_URI, True, "request"),
        (alice, STANDING_URI, True, "signal"),
        (alice, STANDING_URI, False, "signal"),
    ]


def test_a_reopened_listen_within_the_allowance_keeps_the_answer_yes_and_turns_nothing(
    signalling_front: McpPersonFront,
    signalling_changes: ResourceChanges,
    host: ListeningHost,
    alice: Principal,
) -> None:
    first = listen_connection(signalling_front, alice, [STANDING_URI])
    second = listen_connection(signalling_front, alice, [STANDING_URI], request_id=2)

    async def scenario() -> list[bool]:
        answers = []
        async with signalling_front.running():
            async with open_stream(first):
                pass
            answers.append(await signalling_changes.listens_to(alice, STANDING_URI))
            await asyncio.sleep(LISTEN_ALLOWANCE_SECONDS / 2)
            async with open_stream(second):
                answers.append(await signalling_changes.listens_to(alice, STANDING_URI))
                await asyncio.sleep(_PAST_THE_ALLOWANCE)
                answers.append(await signalling_changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, True, True]
    assert [(listens, task) for _p, _u, listens, task in host.calls] == [
        (True, "request"),
        (True, "signal"),
    ]


@pytest.mark.parametrize(
    "staying_seconds",
    [
        pytest.param(0, id="the client leaves"),
        pytest.param(LISTEN_BOUND_SECONDS + 1, id="the bound"),
    ],
)
def test_a_listen_nothing_follows_turns_the_answer_no_after_the_allowance_with_one_call(
    signalling_front: McpPersonFront,
    signalling_changes: ResourceChanges,
    host: ListeningHost,
    alice: Principal,
    staying_seconds: float,
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        answers = []
        async with signalling_front.running():
            async with open_stream(connection):
                await asyncio.sleep(staying_seconds)
            await asyncio.sleep(LISTEN_ALLOWANCE_SECONDS / 2)
            answers.append(await signalling_changes.listens_to(alice, STANDING_URI))
            await asyncio.sleep(LISTEN_ALLOWANCE_SECONDS)
            answers.append(await signalling_changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, False]
    assert [(listens, task) for _p, _u, listens, task in host.calls] == [
        (True, "request"),
        (True, "signal"),
        (False, "signal"),
    ]


def test_the_first_covering_stream_turns_yes_and_a_second_turns_nothing(
    signalling_front: McpPersonFront, host: ListeningHost, alice: Principal, bob: Principal
) -> None:
    streams = [
        listen_connection(signalling_front, alice, [STANDING_URI], request_id=1),
        listen_connection(signalling_front, alice, [STANDING_URI], request_id=2),
        listen_connection(signalling_front, bob, [STANDING_URI], request_id=3),
    ]

    async def scenario() -> list[int]:
        seen = []
        async with signalling_front.running(), open_stream(streams[0]):
            seen.append(len(host.calls))
            async with open_stream(streams[1]):
                seen.append(len(host.calls))
                async with open_stream(streams[2]):
                    seen.append(len(host.calls))
        return seen

    assert served(scenario()) == [2, 2, 4]
    assert [(principal, task) for principal, _u, _l, task in host.calls] == [
        (alice, "request"),
        (alice, "signal"),
        (bob, "request"),
        (bob, "signal"),
    ]


def test_a_stream_calls_yes_only_once_acknowledged(
    signalling_front: McpPersonFront, app: MemberApp, host: ListeningHost, alice: Principal
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])
    app.current_seconds = 5

    async def scenario() -> tuple[int, int]:
        async with signalling_front.running(), open_stream(connection):
            before = len(host.calls)
            await asyncio.sleep(5)
            return before, len(host.calls)

    assert served(scenario()) == (0, 2)


def test_after_an_older_no_the_host_that_follows_the_signal_task_ends_up_listing_the_agent(
    signalling_front: McpPersonFront,
    host: ListeningHost,
    place_store: PlaceStoreFake,
    alice: Principal,
) -> None:
    first = listen_connection(signalling_front, alice, [STANDING_URI], request_id=1)
    second = listen_connection(signalling_front, alice, [STANDING_URI], request_id=2)
    host.on_call = lambda task: place_store.release_signals() if task == "request" else None

    async def scenario() -> None:
        async with signalling_front.running():
            async with open_stream(first):
                pass
            place_store.hold_signals()
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            async with open_stream(second):
                pass

    served(scenario())

    assert [(listens, task) for _p, _u, listens, task in host.calls[2:]] == [
        (True, "request"),
        (False, "signal"),
        (True, "signal"),
    ]
    assert host.followed == {(alice, STANDING_URI): True}


def test_a_failing_acknowledgement_answers_the_fixed_text_and_calls_nothing_on_the_request_task(
    signalling_front: McpPersonFront,
    host: ListeningHost,
    place_store: PlaceStoreFake,
    alice: Principal,
) -> None:
    place_store.acknowledged_failure = PlaceStoreError("the reply was lost")
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running():
            async with open_stream(connection):
                pass
            await asyncio.sleep(_PAST_THE_ALLOWANCE)

    served(scenario())

    reply = json.loads(connection.body_bytes)
    assert (reply["error"]["code"], reply["error"]["message"]) == (-32603, _FAILED_TEXT)
    assert [(listens, task) for _p, _u, listens, task in host.calls] == [
        (True, "signal"),
        (False, "signal"),
    ]
    assert "ended" in place_store.shared.stream_calls


def test_a_failing_end_is_logged_by_type_and_the_listen_ends_normally(
    signalling_front: McpPersonFront,
    place_store: PlaceStoreFake,
    alice: Principal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    place_store.ended_failure = PlaceStoreError("the secret detail")
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running(), open_stream(connection):
            pass

    served(scenario())

    assert connection.status == 200
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "PlaceStoreError" in logged
    assert "secret detail" not in logged


def test_a_listen_whose_task_is_cancelled_still_ends_its_stream_in_the_store(
    signalling_front: McpPersonFront, place_store: PlaceStoreFake, alice: Principal
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running():
            task = asyncio.create_task(connection.run())
            await settled()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await settled()

    served(scenario())

    assert place_store.shared.stream_calls == ["acknowledged", "ended"]


def test_a_failing_lapse_is_logged_and_the_next_tick_lapses(
    signalling_front: McpPersonFront,
    host: ListeningHost,
    place_store: PlaceStoreFake,
    alice: Principal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        async with signalling_front.running():
            async with open_stream(connection):
                pass
            place_store.lapse_failure = PlaceStoreError("the store is down")
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            failing = [listens for _p, _u, listens, _t in host.calls]
            place_store.lapse_failure = None
            await asyncio.sleep(2 * LAPSE_POLL_SECONDS)
            return [*failing, host.calls[-1][2]]

    assert served(scenario()) == [True, True, False]
    assert "PlaceStoreError" in " ".join(record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("read", ["listens_to", "listeners"])
def test_a_failing_store_raises_place_store_error_from_a_read(
    changes: ResourceChanges, place_store: PlaceStoreFake, alice: Principal, read: str
) -> None:
    place_store.read_failure = PlaceStoreError("the store did not answer")

    async def scenario() -> None:
        if read == "listens_to":
            await changes.listens_to(alice, STANDING_URI)
        else:
            await changes.listeners(STANDING_URI)

    with pytest.raises(PlaceStoreError):
        served(scenario())


def test_a_callable_that_raises_leaves_the_listen_unharmed_and_is_logged_by_type_only(
    app: MemberApp,
    alice: Principal,
    place_store: PlaceStoreFake,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    def raises(_principal: Principal, _uri: str, _listens: bool) -> None:
        raise RuntimeError("the host's secret detail")

    changes = ResourceChanges(place_store, listening_changed=raises)
    front = McpPersonFront([], resolve_person=resolve_member, resources=app, changes=changes)
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        answers = []
        async with front.running():
            async with open_stream(connection):
                answers.append(await changes.listens_to(alice, STANDING_URI))
                await changes.changed(alice, STANDING_URI)
                await settled()
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            answers.append(await changes.listens_to(alice, STANDING_URI))
        return answers

    assert served(scenario()) == [True, False]
    assert connection.updated_uris == [STANDING_URI]
    assert connection.status == 200
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "listening_changed" in logged
    assert "RuntimeError" in logged
    assert "resource method" not in logged
    assert "secret detail" not in logged
