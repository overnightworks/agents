"""Whether a principal listens to a URI: the answer ``ResourceChanges.listens_to`` gives an app,
and the calls ``listening_changed`` makes on every pod."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.listen import LAPSE_POLL_SECONDS
from agents.mcp_server.place_store import PlaceStoreError
from agents.member import LISTEN_ALLOWANCE_SECONDS, LISTEN_BOUND_SECONDS, STANDING_URI
from agents.tools import Principal
from tests.support import listen_support
from tests.support.listen_support import ListeningHost
from tests.support.log_lines import LogLines, log_lines
from tests.support.mcp_server_support import (
    MemberApp,
    listen_connection,
    open_stream,
    resolve_member,
    settled,
    window_uri,
)
from tests.support.place_store_support import PlaceStoreFake, refused_with_a_cause

alice = listen_support.alice
bob = listen_support.bob
app = listen_support.app
changes = listen_support.changes
front = listen_support.front
served = listen_support.served
place_store = listen_support.place_store
log_lines = log_lines

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
    log_lines: LogLines,
) -> None:
    place_store.acknowledged_failure = refused_with_a_cause()
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
    failed = log_lines.one("listen_acknowledgement.ended")
    assert (
        failed["outcome"],
        failed["level"],
        failed["error_type"],
        failed["cause_type"],
        failed["uri_count"],
    ) == ("failed", "warning", "PlaceStoreError", "PermissionError", 1)
    assert "turned_count" not in failed
    assert log_lines.of("listening.opened") == []


def test_an_acknowledgement_is_one_ok_line_with_the_uris_and_those_it_turned_to_yes(
    front: McpPersonFront, alice: Principal, log_lines: LogLines
) -> None:
    first = listen_connection(front, alice, [STANDING_URI, window_uri("first")], request_id=1)
    second = listen_connection(front, alice, [STANDING_URI], request_id=2)

    async def scenario() -> None:
        async with front.running(), open_stream(first), open_stream(second):
            pass

    served(scenario())

    assert [
        (line["outcome"], line["uri_count"], line["turned_count"])
        for line in log_lines.of("listen_acknowledgement.ended")
    ] == [("ok", 2, 2), ("ok", 1, 0)]
    assert [line["sub"] for line in log_lines.of("listening.opened")] == [alice.sub] * 2


def test_a_failing_end_is_one_failed_line_without_the_detail_and_the_listen_ends_normally(
    signalling_front: McpPersonFront,
    place_store: PlaceStoreFake,
    alice: Principal,
    log_lines: LogLines,
) -> None:
    place_store.ended_failure = refused_with_a_cause()
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running(), open_stream(connection):
            pass

    served(scenario())

    ended = log_lines.one("listen_end.ended")
    assert connection.status == 200
    assert (ended["outcome"], ended["level"], ended["error_type"], ended["cause_type"]) == (
        "failed",
        "warning",
        "PlaceStoreError",
        "PermissionError",
    )
    assert "secret detail" not in log_lines.everything()


def test_an_end_that_is_recorded_is_one_ok_line(
    signalling_front: McpPersonFront, alice: Principal, log_lines: LogLines
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running(), open_stream(connection):
            pass

    served(scenario())

    assert log_lines.one("listen_end.ended")["outcome"] == "ok"


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


@pytest.mark.parametrize(
    ("failure", "logged_type"),
    [
        pytest.param(PlaceStoreError("the store is down"), "PlaceStoreError", id="a store error"),
        pytest.param(refused_with_a_cause(), "PermissionError", id="a refusal with its cause"),
        pytest.param(RuntimeError("the secret detail"), "RuntimeError", id="an unexpected error"),
    ],
)
def test_a_failing_lapse_writes_one_failed_line_by_type_and_the_next_tick_lapses(
    signalling_front: McpPersonFront,
    host: ListeningHost,
    place_store: PlaceStoreFake,
    alice: Principal,
    log_lines: LogLines,
    failure: Exception,
    logged_type: str,
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> list[bool]:
        async with signalling_front.running():
            async with open_stream(connection):
                pass
            place_store.lapse_failure = failure
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            failing = [listens for _p, _u, listens, _t in host.calls]
            place_store.lapse_failure = None
            await asyncio.sleep(2 * LAPSE_POLL_SECONDS)
            return [*failing, host.calls[-1][2]]

    assert served(scenario()) == [True, True, False]
    ran = log_lines.of("listen_lapse.ended")
    assert [line["outcome"] for line in ran] == ["failed", "ok"]
    assert (ran[0]["level"], ran[0]["last_error_type"]) == ("warning", logged_type)
    assert "secret detail" not in log_lines.everything()


def test_a_lapse_run_that_fails_for_several_polls_writes_its_first_failure_and_then_its_recovery(
    signalling_front: McpPersonFront, place_store: PlaceStoreFake, log_lines: LogLines
) -> None:
    failed_polls = 3

    async def scenario() -> None:
        async with signalling_front.running():
            place_store.lapse_failure = PlaceStoreError("the store is down")
            await asyncio.sleep(failed_polls * LAPSE_POLL_SECONDS + 0.5)
            place_store.lapse_failure = None
            await asyncio.sleep(2 * LAPSE_POLL_SECONDS)

    served(scenario())

    assert [(line["event"], line["level"]) for line in log_lines.written_by("agents")] == [
        ("listen_lapse.ended", "warning"),
        ("listen_lapse.resumed", "info"),
    ]
    assert log_lines.one("listen_lapse.resumed")["failed_run_count"] == failed_polls


def test_a_lapse_run_in_which_one_stream_failed_is_one_failed_line_with_its_counts(
    signalling_front: McpPersonFront, place_store: PlaceStoreFake, log_lines: LogLines
) -> None:
    place_store.lapse_item_failure = "StoreError"

    async def scenario() -> None:
        async with signalling_front.running():
            await asyncio.sleep(LAPSE_POLL_SECONDS + 0.5)

    served(scenario())

    failed = log_lines.one("listen_lapse.ended")
    assert (failed["outcome"], failed["reason"], failed["failed_count"]) == (
        "failed",
        "lapse_failed",
        1,
    )
    assert failed["last_error_type"] == "StoreError"


async def _idle_front(front: McpPersonFront, seconds: float) -> None:
    async with front.running():
        await asyncio.sleep(seconds)


def test_a_lapse_run_that_found_nothing_writes_no_line(
    signalling_front: McpPersonFront, log_lines: LogLines
) -> None:
    served(_idle_front(signalling_front, 5 * LAPSE_POLL_SECONDS))

    assert log_lines.written_by("agents") == []


def test_a_lapse_writes_one_line_with_its_counts_and_one_lapsed_line_for_the_principal(
    signalling_front: McpPersonFront, alice: Principal, log_lines: LogLines
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running():
            async with open_stream(connection):
                pass
            await asyncio.sleep(_PAST_THE_ALLOWANCE)

    served(scenario())

    ran = log_lines.one("listen_lapse.ended")
    assert (ran["outcome"], ran["stream_count"], ran["turn_count"], ran["failed_count"]) == (
        "ok",
        1,
        1,
        0,
    )
    assert log_lines.one("listening.lapsed")["sub"] == alice.sub


def test_each_lapse_run_writes_its_lines_in_one_trace_of_its_own(
    signalling_front: McpPersonFront,
    place_store: PlaceStoreFake,
    alice: Principal,
    log_lines: LogLines,
) -> None:
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with signalling_front.running():
            async with open_stream(connection):
                pass
            place_store.lapse_failure = PlaceStoreError("the store is down")
            await asyncio.sleep(_PAST_THE_ALLOWANCE)
            place_store.lapse_failure = None
            await asyncio.sleep(2 * LAPSE_POLL_SECONDS)

    served(scenario())

    failed, ended = log_lines.of("listen_lapse.ended")
    trace_of_failed_run = failed["trace_id"]
    trace_of_ended_run = ended["trace_id"]
    assert trace_of_failed_run
    assert trace_of_ended_run
    assert trace_of_failed_run != trace_of_ended_run
    assert log_lines.one("listen_lapse.resumed")["trace_id"] == trace_of_ended_run
    assert log_lines.one("listening.lapsed")["trace_id"] == trace_of_ended_run


def test_two_pods_and_one_acknowledgement_write_one_opened_line_and_one_lapsed_line(
    app: MemberApp,
    alice: Principal,
    place_store: PlaceStoreFake,
    log_lines: LogLines,
) -> None:
    front_a, _host_a = a_front(app, place_store)
    front_b, _host_b = a_front(app, place_store.shared.pod())
    connection = listen_connection(front_a, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front_a.running(), front_b.running():
            async with open_stream(connection):
                await settled()
            await asyncio.sleep(_PAST_THE_ALLOWANCE)

    served(scenario())

    assert log_lines.one("listening.opened")["sub"] == alice.sub
    assert log_lines.one("listening.lapsed")["sub"] == alice.sub


def test_a_listen_cancelled_as_the_front_stops_has_its_end_recorded_before_running_exits(
    signalling_front: McpPersonFront, place_store: PlaceStoreFake, alice: Principal
) -> None:
    place_store.ended_takes = 1
    connection = listen_connection(signalling_front, alice, [STANDING_URI])

    async def scenario() -> list[str]:
        async with signalling_front.running():
            task = asyncio.create_task(connection.run())
            await settled()
            task.cancel()
        return place_store.shared.stream_calls

    assert served(scenario()) == ["acknowledged", "ended"]


@pytest.mark.parametrize(
    ("read", "event"),
    [("listens_to", "listening_lookup.ended"), ("listeners", "listeners_lookup.ended")],
)
def test_a_failing_store_raises_place_store_error_from_a_read_and_writes_one_failed_line(
    changes: ResourceChanges,
    place_store: PlaceStoreFake,
    alice: Principal,
    read: str,
    event: str,
    log_lines: LogLines,
) -> None:
    place_store.read_failure = refused_with_a_cause()

    async def scenario() -> None:
        if read == "listens_to":
            await changes.listens_to(alice, STANDING_URI)
        else:
            await changes.listeners(STANDING_URI)

    read_scenario = scenario()

    with pytest.raises(PlaceStoreError):
        served(read_scenario)

    failed = log_lines.one(event)
    assert (failed["outcome"], failed["error_type"], failed["cause_type"]) == (
        "failed",
        "PlaceStoreError",
        "PermissionError",
    )


def test_a_callable_that_raises_leaves_the_listen_unharmed_and_is_one_error_line_by_type_only(
    app: MemberApp,
    alice: Principal,
    place_store: PlaceStoreFake,
    log_lines: LogLines,
) -> None:
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
    failed = log_lines.of("host_callback.failed")
    assert {(line["level"], line["callback"], line["error_type"]) for line in failed} == {
        ("error", "listening_changed", "RuntimeError")
    }
    assert "secret detail" not in log_lines.everything()


def test_a_listens_to_read_is_one_ok_line(
    front: McpPersonFront, alice: Principal, changes: ResourceChanges, log_lines: LogLines
) -> None:
    connection = listen_connection(front, alice, [STANDING_URI])

    async def scenario() -> None:
        async with front.running(), open_stream(connection):
            await changes.listens_to(alice, STANDING_URI)

    served(scenario())

    assert log_lines.one("listening_lookup.ended")["outcome"] == "ok"


def test_a_listeners_read_is_one_ok_line_with_the_count_of_principals(
    front: McpPersonFront,
    alice: Principal,
    bob: Principal,
    changes: ResourceChanges,
    log_lines: LogLines,
) -> None:
    streams = [
        listen_connection(front, alice, [STANDING_URI], request_id=1),
        listen_connection(front, bob, [STANDING_URI], request_id=2),
    ]

    async def scenario() -> None:
        async with front.running(), open_stream(streams[0]), open_stream(streams[1]):
            await changes.listeners(STANDING_URI)

    served(scenario())

    ended = log_lines.one("listeners_lookup.ended")
    assert (ended["outcome"], ended["principal_count"]) == ("ok", 2)
