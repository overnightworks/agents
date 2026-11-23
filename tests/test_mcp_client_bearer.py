"""What of the bearer leaves a member session, and how a bearer that is no header value fails.

The session runs against an app's front on a loopback socket, as in ``test_mcp_client_session``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from http import HTTPStatus

import pytest
from pydantic import SecretStr

from agents.mcp_client import MemberSession, open_member_session
from agents.member_outcomes import CallFailure, UnusableAnswer
from tests.support import member_session_support
from tests.support.mcp_server_support import MemberApp
from tests.support.member_session_support import (
    SUBJECT,
    answering,
    logged_outside_the_wire_trace,
    opened,
    reads_the_standing,
    stands,
)
from tests.support.scripted_gate import READ, Handler, ScriptedGate, cuts

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
gate = member_session_support.gate
bearer = member_session_support.bearer

# What leaves a session of the bearer.


async def test_a_content_type_that_reflects_the_bearer_reaches_no_log_line_but_the_wire_trace(
    app: MemberApp, gate: ScriptedGate, caplog: pytest.LogCaptureFixture
) -> None:
    stands(app)
    bearer = SecretStr(secrets.token_hex(12))
    gate.let_in(bearer.get_secret_value(), SUBJECT)
    reflected = f"application/{bearer.get_secret_value()}"
    gate.script(READ, lambda reply: reply.body(HTTPStatus.OK, reflected, b"{}"))

    with pytest.raises(UnusableAnswer):
        await reads_the_standing(gate, bearer)

    assert bearer.get_secret_value() not in logged_outside_the_wire_trace(caplog)


def test_the_log_check_reads_a_bearer_that_only_a_logged_exception_holds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bearer = secrets.token_hex(12)
    try:
        raise ValueError(bearer)
    except ValueError:
        logging.getLogger("agents.test").exception("the call failed")

    assert bearer in logged_outside_the_wire_trace(caplog)


@pytest.mark.parametrize(
    "shape",
    ["", "first-{}\nsecond", "{}\r\nX-Injected: yes", "{}\x00", "{} "],
    ids=["empty", "a newline", "a CR LF", "a control character", "a trailing space"],
)
async def test_a_bearer_that_is_no_header_value_is_refused_before_any_request_and_named_nowhere(
    gate: ScriptedGate, shape: str
) -> None:
    marker = secrets.token_hex(12)
    refused = SecretStr(shape.format(marker))

    with pytest.raises(ValueError, match="bearer") as caught:
        await reads_the_standing(gate, refused)

    chain: list[BaseException] = []
    error: BaseException | None = caught.value
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert not gate.seen
    assert marker not in "".join(repr(each) + str(each) for each in chain)
    assert not any(isinstance(each, CallFailure) for each in chain)


REQUESTS = [
    pytest.param(lambda session: session.read_standing(), id="a read"),
    pytest.param(lambda session: session.declared_tools(), id="a tool list"),
]
CALLS = [*REQUESTS, pytest.param(lambda session: session.listen().__aenter__(), id="a listen")]


@pytest.mark.parametrize("call", CALLS)
async def test_a_bearer_renewed_into_a_malformed_value_fails_the_next_call_alone(
    app: MemberApp,
    gate: ScriptedGate,
    bearer: SecretStr,
    call: Callable[[MemberSession], Awaitable[object]],
) -> None:
    stands(app)
    current = bearer
    task = asyncio.current_task()
    assert task is not None

    async with open_member_session(gate.url, lambda: current) as session:
        current = SecretStr("renewed\nbadly")
        with pytest.raises(ValueError, match="bearer"):
            await call(session)
        current = bearer
        standing = await session.read_standing()

    assert standing.standings
    assert task.cancelling() == 0


async def reads_the_standing_showing(
    gate: ScriptedGate, bearer: SecretStr, shown: list[str]
) -> None:
    async with opened(gate, bearer) as session:
        shown.append(repr(session) + str(session))
        await session.read_standing()


@pytest.mark.parametrize(
    "script",
    [
        pytest.param(answering(HTTPStatus.SERVICE_UNAVAILABLE), id="a refused call"),
        pytest.param(lambda reply: reply.raw_result({"contents": 7}), id="an answer in no shape"),
        pytest.param(cuts, id="a cut connection"),
        pytest.param(None, id="an unknown bearer"),
    ],
)
async def test_no_bearer_is_in_any_failure_or_in_the_session(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, script: Handler | None
) -> None:
    stands(app)
    if script is None:
        bearer = SecretStr(secrets.token_urlsafe(24))
    else:
        gate.script(READ, script)
    shown: list[str] = []

    with pytest.raises(CallFailure) as caught:
        await reads_the_standing_showing(gate, bearer, shown)
    failure = caught.value
    shown.append(repr(failure) + str(failure) + repr(failure.__traceback__))

    assert bearer.get_secret_value() not in "".join(shown)
    assert shown
