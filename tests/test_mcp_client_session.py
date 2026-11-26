"""The member session against an app's front on a loopback socket: the real wire.

An app's front answers through a scripted gate, which misbehaves where a test says so and lets every
other request through to the library's own front.
"""

from __future__ import annotations

import asyncio
import secrets
from http import HTTPStatus
from typing import Any

import httpx2
import pytest
from logline import current_traceparent, unit_trace
from logline.testing import configured
from mcp_types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    UNSUPPORTED_PROTOCOL_VERSION,
    BlobResourceContents,
    DiscoverResult,
    ReadResourceResult,
    ServerCapabilities,
    TextResourceContents,
)
from pydantic import SecretStr

from agents.mcp_client import READ_SECONDS, open_member_session
from agents.member import (
    LEASE_SECONDS,
    STANDING_URI,
    MemberStanding,
    PartialSpeech,
    PlaceRef,
    StandingState,
    TalkedOver,
    VoiceState,
)
from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    UNSUPPORTED_VERSION_CODE,
    StatusFailure,
    Unreachable,
    UnusableAnswer,
    jsonrpc_code,
    status_code,
)
from tests.support import member_session_support
from tests.support.mcp_server_support import (
    MemberApp,
    a_member,
    a_standing,
    a_view,
    view_uri,
    window_uri,
)
from tests.support.member_session_support import (
    LIMIT_SECONDS,
    ROOM,
    SUBJECT,
    a_window,
    answering,
    opened,
    reads_the_standing,
    reads_the_window,
    stands,
)
from tests.support.scripted_gate import (
    DISCOVER,
    NO_METHOD,
    READ,
    GateReply,
    ScriptedGate,
    cuts,
    hangs,
)
from tests.support.voice_lines import a_line

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
gate = member_session_support.gate
bearer = member_session_support.bearer
place = member_session_support.place
table = member_session_support.table


# What the session reads.


async def test_a_read_answers_every_standing_of_the_member(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    app.readable[a_member(SUBJECT)] = {
        STANDING_URI: MemberStanding(
            standings=(
                a_standing(ROOM, StandingState.INVITED),
                a_standing("room-8", StandingState.ACTIVE),
            )
        )
    }

    async with opened(gate, bearer) as session:
        standing = await session.read_standing()

    assert [(each.place.id, each.state.value) for each in standing.standings] == [
        (ROOM, "invited"),
        ("room-8", "active"),
    ]


async def test_a_window_holds_the_lines_of_the_place_and_marks_the_members_own(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    heard, own = secrets.token_hex(4), secrets.token_hex(4)
    stands(app, **{window_uri(ROOM): a_window(a_line(1, heard), a_line(2, own, own=True))})

    async with opened(gate, bearer) as session:
        window = await session.read_place(place)

    assert window is not None
    assert [(each.seq, each.text, each.own) for each in window.entries] == [
        (1, heard, False),
        (2, own, True),
    ]


async def test_a_window_with_an_act_line_is_read_with_the_mark_kept(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    stands(
        app,
        **{
            window_uri(ROOM): a_window(
                a_line(1, "took a card", spoken=False, act=True),
                a_line(2, "good luck", spoken=False),
            )
        },
    )

    async with opened(gate, bearer) as session:
        window = await session.read_place(place)

    assert window is not None
    assert [(each.text, each.act) for each in window.entries] == [
        ("took a card", True),
        ("good luck", False),
    ]


async def test_a_window_with_a_voice_state_a_cut_line_and_people_talking_over_him_is_read_whole(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    window = a_window(
        a_line(1, "heard", cut=True),
        talked_over=TalkedOver(
            id="talk-1", playing=True, speech=(PartialSpeech(name="Ada", text="but"),)
        ),
        voice_state=VoiceState.UNAVAILABLE,
    )
    stands(app, **{window_uri(ROOM): window})

    async with opened(gate, bearer) as session:
        read = await session.read_place(place)

    assert read == window


@pytest.mark.parametrize("arrangement", ["not in the place", "answered inside a 200"])
async def test_a_window_the_app_shows_the_member_no_more_is_none(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef, arrangement: str
) -> None:
    stands(app)
    if arrangement == "answered inside a 200":
        stands(app, **{window_uri(ROOM): a_window()})
        gate.script(READ, lambda reply: reply.error(INVALID_PARAMS, status=HTTPStatus.OK))

    async with opened(gate, bearer) as session:
        window = await session.read_place(place)

    assert window is None


async def test_a_window_read_the_app_refuses_is_a_failure_and_no_missing_window(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    stands(app, **{window_uri(ROOM): a_window()})
    gate.script(READ, answering(HTTPStatus.FORBIDDEN))

    with pytest.raises(StatusFailure):
        await reads_the_window(gate, bearer, place)


@pytest.mark.parametrize(
    "contents",
    [
        TextResourceContents(uri=window_uri(ROOM), text="{}", mime_type="application/json"),
        BlobResourceContents(uri=window_uri(ROOM), blob="e30=", mime_type="application/json"),
    ],
    ids=["not a window", "not text"],
)
async def test_a_window_that_is_no_window_is_an_unusable_answer(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef, contents: Any
) -> None:
    async def answers_wrongly(reply: GateReply) -> None:
        await reply.result(ReadResourceResult(contents=[contents], ttl_ms=0, cache_scope="private"))

    stands(app, **{window_uri(ROOM): a_window()})
    gate.script(READ, answers_wrongly)

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_window(gate, bearer, place)

    assert failure.value.code == INVALID_ANSWER_CODE


async def test_a_read_the_app_answers_with_a_request_for_input_is_an_unusable_answer(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.script(
        READ, lambda reply: reply.raw_result({"resultType": "input_required", "requestState": "x"})
    )

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.code == INVALID_ANSWER_CODE


async def test_a_view_holds_the_revision_the_wait_and_the_state_as_the_app_sent_them(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, table: PlaceRef
) -> None:
    sent = a_view(revision=7).model_copy(update={"awaits_you": True})
    stands(app, **{view_uri(table.id): sent})

    async with opened(gate, bearer) as session:
        view = await session.read_view(table)

    assert view == sent


@pytest.mark.parametrize("arrangement", ["not in the place", "answered inside a 200"])
async def test_a_view_the_app_shows_the_member_no_more_is_none(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, table: PlaceRef, arrangement: str
) -> None:
    stands(app)
    if arrangement == "answered inside a 200":
        stands(app, **{view_uri(table.id): a_view()})
        gate.script(READ, lambda reply: reply.error(INVALID_PARAMS, status=HTTPStatus.OK))

    async with opened(gate, bearer) as session:
        view = await session.read_view(table)

    assert view is None


async def test_a_view_that_is_no_view_is_an_unusable_answer(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, table: PlaceRef
) -> None:
    async def answers_wrongly(reply: GateReply) -> None:
        contents = TextResourceContents(uri=STANDING_URI, text="{}", mime_type="application/json")
        await reply.result(ReadResourceResult(contents=[contents], ttl_ms=0, cache_scope="private"))

    stands(app, **{view_uri(table.id): a_view()})
    gate.script(READ, answers_wrongly)

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.read_view(table)

    assert failure.value.code == INVALID_ANSWER_CODE


async def test_two_calls_at_once_each_see_the_answer_of_their_own_request(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    """The standing is refused with a status while the window's connection is cut after that."""
    refused = asyncio.Event()

    async def either_one(reply: GateReply) -> None:
        if reply.rpc.params["uri"] == STANDING_URI:
            await reply.status(HTTPStatus.SERVICE_UNAVAILABLE)
            refused.set()
        else:
            await refused.wait()
            await reply.cut()

    stands(app, **{window_uri(ROOM): a_window()})
    gate.script(READ, either_one, either_one)

    async with opened(gate, bearer) as session:
        async with asyncio.timeout(LIMIT_SECONDS):
            standing, window = await asyncio.gather(
                session.read_standing(), session.read_place(place), return_exceptions=True
            )

    assert isinstance(standing, StatusFailure)
    assert standing.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert isinstance(window, Unreachable)


# What the session sends.


async def test_a_renewed_token_is_the_bearer_of_the_next_call(
    app: MemberApp, gate: ScriptedGate
) -> None:
    stands(app)
    first, renewed = (SecretStr(secrets.token_urlsafe(24)) for _ in range(2))
    gate.let_in(first.get_secret_value(), SUBJECT)
    gate.let_in(renewed.get_secret_value(), SUBJECT)
    current = [first]

    async with open_member_session(gate.url, lambda: current[0]) as session:
        await session.read_standing()
        before = len(gate.seen)
        del gate.bearers[first.get_secret_value()]
        current[0] = renewed
        await session.read_standing()

    assert {each.bearer for each in gate.seen[:before]} == {first.get_secret_value()}
    assert {each.bearer for each in gate.seen[before:]} == {renewed.get_secret_value()}


async def test_a_bearer_the_app_does_not_know_is_a_401_of_the_discovery(
    gate: ScriptedGate,
) -> None:
    unknown = SecretStr(secrets.token_urlsafe(24))

    with pytest.raises(StatusFailure) as failure:
        await reads_the_standing(gate, unknown)

    assert (failure.value.status, failure.value.method) == (HTTPStatus.UNAUTHORIZED, DISCOVER)


@pytest.mark.parametrize("inside_a_trace", [True, False], ids=["inside a trace", "outside one"])
async def test_the_trace_of_the_caller_travels_with_every_request(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, inside_a_trace: bool
) -> None:
    stands(app)

    with configured("agents-tests"):
        if inside_a_trace:
            with unit_trace(None):
                traceparent = current_traceparent()
                await reads_the_standing(gate, bearer)
        else:
            traceparent = None
            await reads_the_standing(gate, bearer)

    assert {each.traceparent for each in gate.seen} == {traceparent}


async def test_a_transport_the_caller_passes_carries_every_request(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    carried: list[str] = []

    class Counting(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self.real = httpx2.AsyncHTTPTransport()

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            carried.append(request.headers["authorization"])
            return await self.real.handle_async_request(request)

    async with opened(gate, bearer, transport=Counting()) as session:
        await session.read_standing()

    assert carried
    assert len(carried) == len(gate.seen)


# The HTTP policy.


async def test_the_proxy_variables_of_the_environment_do_not_reroute_a_request(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, monkeypatch: pytest.MonkeyPatch
) -> None:
    stands(app)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")

    await reads_the_standing(gate, bearer)

    assert gate.count(READ) == 1


@pytest.mark.parametrize("status", [HTTPStatus.FOUND, HTTPStatus.TEMPORARY_REDIRECT])
async def test_a_redirect_is_an_unusable_answer_named_by_its_status_and_not_followed(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, status: HTTPStatus
) -> None:
    stands(app)
    elsewhere = ScriptedGate(MemberApp())
    async with elsewhere.serving():

        async def redirects(reply: GateReply) -> None:
            await reply.status(status, {"Location": elsewhere.url})

        gate.script(READ, redirects)

        with pytest.raises(UnusableAnswer) as failure:
            await reads_the_standing(gate, bearer)

    assert failure.value.code == status_code(status)
    assert elsewhere.seen == []


@pytest.mark.parametrize("status", [HTTPStatus.TEMPORARY_REDIRECT, HTTPStatus.PERMANENT_REDIRECT])
async def test_a_redirect_to_the_apps_own_origin_is_not_followed_either(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, status: HTTPStatus
) -> None:
    stands(app)

    async def redirects(reply: GateReply) -> None:
        await reply.status(status, {"Location": gate.url})

    gate.script(READ, redirects)

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.code == status_code(status)
    assert gate.count(READ) == 1


async def test_a_cookie_the_app_sets_is_never_sent_back(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.sets_cookie = "route=pod-1; Path=/"

    async with opened(gate, bearer) as session:
        await session.read_standing()
        await session.read_standing()

    assert len(gate.seen) > 2
    assert {each.cookie for each in gate.seen} == {None}


# How a call fails.


def a_page() -> bytes:
    return f"<html>{secrets.token_hex(12)}</html>".encode()


def answering_with(content_type: str | None, content: bytes) -> Any:
    async def answer(reply: GateReply, status: HTTPStatus) -> None:
        await reply.body(status, content_type, content)

    return answer


async def answering_with_a_jsonrpc_error(reply: GateReply, status: HTTPStatus) -> None:
    await reply.error(INTERNAL_ERROR, status=status)


ANSWERS = [
    pytest.param(answering_with_a_jsonrpc_error, jsonrpc_code(INTERNAL_ERROR), id="jsonrpc error"),
    pytest.param(
        answering_with("text/html; charset=utf-8", b"<html></html>"), "text/html", id="html"
    ),
    pytest.param(
        answering_with("application/json", b'{"detail": "no"}'),
        "application/json",
        id="json that is no jsonrpc error",
    ),
    pytest.param(
        answering_with("application/json", b'{"jsonrpc": "2.0", "id": 1, "result": {}}'),
        "application/json",
        id="json that is a jsonrpc result",
    ),
    pytest.param(answering_with(None, b"<html></html>"), None, id="no content type"),
    pytest.param(answering_with("not a media type", b"<html></html>"), None, id="no media type"),
    pytest.param(answering_with(None, b""), None, id="empty"),
]


@pytest.mark.parametrize(
    "status",
    [
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.SERVICE_UNAVAILABLE,
    ],
)
@pytest.mark.parametrize(("answers", "answer"), ANSWERS)
async def test_a_status_failure_names_the_method_and_the_answers_kind_and_none_of_its_content(
    app: MemberApp,
    gate: ScriptedGate,
    bearer: SecretStr,
    place: PlaceRef,
    status: HTTPStatus,
    answers: Any,
    answer: str | None,
) -> None:
    stands(app, **{window_uri(ROOM): a_window()})

    async def scripted(reply: GateReply) -> None:
        await answers(reply, status)

    gate.script(READ, scripted)
    with pytest.raises(StatusFailure) as failure:
        await reads_the_window(gate, bearer, place)

    assert (failure.value.status, failure.value.method, failure.value.answer) == (
        status,
        READ,
        answer,
    )
    assert "html" not in repr(failure.value) + str(failure.value)


async def test_a_status_failure_of_a_stream_reconnect_names_no_method(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def drops_after_an_event_with_an_id(reply: GateReply) -> None:
        await reply.open_stream()
        await reply.event({"jsonrpc": "2.0", "method": "notifications/message"}, event_id="1")
        await reply.end()

    stands(app)
    gate.script(READ, drops_after_an_event_with_an_id)
    gate.script(
        NO_METHOD,
        answering(HTTPStatus.METHOD_NOT_ALLOWED),
        answering(HTTPStatus.METHOD_NOT_ALLOWED),
    )

    with pytest.raises(StatusFailure) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.status == HTTPStatus.METHOD_NOT_ALLOWED
    assert failure.value.method is None


async def test_a_jsonrpc_error_at_a_success_status_is_an_unusable_answer_with_no_method(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.script(READ, lambda reply: reply.error(UNSUPPORTED_PROTOCOL_VERSION, status=HTTPStatus.OK))

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.code == jsonrpc_code(UNSUPPORTED_PROTOCOL_VERSION)
    assert (failure.value.method, failure.value.answer) == (None, None)


@pytest.mark.parametrize("status", [HTTPStatus.FORBIDDEN, HTTPStatus.SERVICE_UNAVAILABLE])
async def test_an_error_answer_whose_body_does_not_decode_is_told_by_its_status(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, status: HTTPStatus
) -> None:
    stands(app)
    gate.script(READ, lambda reply: reply.undecodable(status))

    with pytest.raises(StatusFailure) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.status == status


async def test_an_ok_answer_whose_body_does_not_decode_is_unreachable(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.script(READ, lambda reply: reply.undecodable(HTTPStatus.OK))

    with pytest.raises(Unreachable):
        await reads_the_standing(gate, bearer)


async def test_an_app_that_cannot_be_reached_is_unreachable(app: MemberApp) -> None:
    async with ScriptedGate(app).serving() as gone:
        pass

    with pytest.raises(Unreachable):
        async with open_member_session(gone, lambda: SecretStr("a bearer")):
            pass


async def test_an_app_that_cuts_the_connection_is_unreachable(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.script(READ, cuts)

    with pytest.raises(Unreachable):
        await reads_the_standing(gate, bearer)


async def test_an_app_that_answers_no_more_within_the_read_time_is_unreachable(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.script(READ, hangs)

    with pytest.raises(Unreachable):
        await reads_the_standing(gate, bearer, stream_seconds=0.3)


# How the discovery fails.


async def test_an_app_that_shares_no_protocol_revision_is_an_unsupported_version(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def supports_another_revision(reply: GateReply) -> None:
        discovered = DiscoverResult(
            supported_versions=["1999-01-01"], capabilities=ServerCapabilities()
        )
        await reply.result(discovered)

    gate.script(DISCOVER, supports_another_revision)

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.code == UNSUPPORTED_VERSION_CODE


async def test_an_app_that_refuses_the_revision_with_an_error_status_is_told_by_its_code(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    gate.script(DISCOVER, lambda reply: reply.error(UNSUPPORTED_PROTOCOL_VERSION))

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert (failure.value.code, failure.value.method, failure.value.answer) == (
        jsonrpc_code(UNSUPPORTED_PROTOCOL_VERSION),
        DISCOVER,
        jsonrpc_code(UNSUPPORTED_PROTOCOL_VERSION),
    )


async def test_an_app_that_answers_the_discovery_with_no_discovery_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    gate.script(DISCOVER, lambda reply: reply.raw_result({"supportedVersions": 7}))

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.code == INVALID_ANSWER_CODE


async def test_an_app_that_cuts_the_connection_while_discovering_is_unreachable(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    gate.script(DISCOVER, cuts)

    with pytest.raises(Unreachable):
        await reads_the_standing(gate, bearer)


async def test_a_jsonrpc_error_that_is_no_refused_revision_is_an_unusable_answer_by_its_code(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)
    gate.script(READ, lambda reply: reply.error(INTERNAL_ERROR, status=HTTPStatus.OK))

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_standing(gate, bearer)

    assert failure.value.code == jsonrpc_code(INTERNAL_ERROR)


def test_the_runner_cuts_a_stream_before_the_lease_of_its_member_would_have_ended() -> None:
    assert READ_SECONDS < LEASE_SECONDS
