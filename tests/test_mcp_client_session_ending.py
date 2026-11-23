"""How a member session ends: what the caller's body raises, late answers, cancellation and a close
that loses a child."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any

import anyio
import pytest
from mcp_types import ReadResourceResult, TextResourceContents
from pydantic import SecretStr, ValidationError

from agents.member import STANDING_URI
from agents.member_outcomes import (
    StatusFailure,
    Unreachable,
)
from tests.support import member_session_support
from tests.support.mcp_server_support import (
    MemberApp,
)
from tests.support.member_session_support import (
    LIMIT_SECONDS,
    cancelled_once_entered,
    opened,
    reads_the_standing,
    stands,
)
from tests.support.scripted_gate import (
    CALL,
    DISCOVER,
    READ,
    GateReply,
    ScriptedGate,
    hangs_once_entered,
)
from tests.support.sdk_transport import (
    ClosingHold,
    transport_closing_with_late_answers,
    transport_closing_with_leaves,
    transport_whose_close_loses_a_child,
)

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
gate = member_session_support.gate
bearer = member_session_support.bearer


# What the caller's own code raises.


class BodyError(Exception):
    pass


async def raises_in_the_body(gate: ScriptedGate, bearer: SecretStr, raised: BaseException) -> None:
    async with opened(gate, bearer):
        raise raised


@pytest.mark.parametrize(
    "raised",
    [
        BodyError("the caller's own code failed"),
        OSError("a local file failed"),
        TimeoutError("the caller's own wait timed out"),
        ValidationError.from_exception_data("a local record", []),
        Unreachable("raised by the caller"),
    ],
    ids=["its own error", "os error", "timeout", "validation error", "call failure"],
)
async def test_an_error_raised_in_the_body_reaches_the_caller_unchanged(
    gate: ScriptedGate, bearer: SecretStr, raised: Exception
) -> None:
    with pytest.raises(type(raised)) as caught:
        await raises_in_the_body(gate, bearer, raised)

    assert caught.value is raised


async def raises_after_a_refused_call(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, raised: BaseException
) -> None:
    stands(app)
    async with opened(gate, bearer) as session:
        del gate.bearers[bearer.get_secret_value()]
        with pytest.raises(StatusFailure):
            await session.read_standing()
        raise raised


@pytest.mark.parametrize(
    "own",
    [BodyError("the caller's own code failed"), OSError("a local file failed")],
    ids=["defect", "os error"],
)
async def test_a_leaf_the_body_raises_after_a_4xx_reaches_the_caller_unchanged(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, own: Exception
) -> None:
    raised = ExceptionGroup("own", [own])

    with pytest.raises(type(own)) as caught:
        await raises_after_a_refused_call(app, gate, bearer, raised)

    assert caught.value is own


@pytest.mark.parametrize(
    "own",
    [
        OSError("a local file failed"),
        TimeoutError("the caller's own wait timed out"),
        ValidationError.from_exception_data("a local record", []),
    ],
    ids=["os error", "timeout", "validation error"],
)
async def test_every_leaf_of_nested_groups_the_body_raises_reaches_the_caller(
    gate: ScriptedGate, bearer: SecretStr, own: Exception
) -> None:
    failure, defect = Unreachable("raised by the caller"), BodyError("the caller's own code failed")
    nested = ExceptionGroup("outer", [failure, ExceptionGroup("inner", [own, defect])])

    with pytest.raises(ExceptionGroup) as caught:
        await raises_in_the_body(gate, bearer, nested)

    assert list(_leaves(caught.value)) == [failure, own, defect]


@pytest.mark.parametrize("grouped", [False, True], ids=["alone", "in a group"])
async def test_a_leaf_the_body_raises_keeps_the_cause_it_was_raised_from(
    gate: ScriptedGate, bearer: SecretStr, grouped: bool
) -> None:
    cause = OSError("the file the caller read")
    own = BodyError("the caller's own code failed")
    own.__cause__ = cause
    raised = ExceptionGroup("own", [own]) if grouped else own

    with pytest.raises(BodyError) as caught:
        await raises_in_the_body(gate, bearer, raised)

    assert caught.value is own
    assert caught.value.__cause__ is cause


@pytest.mark.parametrize("grouped", [False, True], ids=["alone", "in a group"])
async def test_a_leaf_the_body_raises_keeps_the_context_it_was_raised_in(
    gate: ScriptedGate, bearer: SecretStr, grouped: bool
) -> None:
    context = KeyError("what the caller was handling")
    own = BodyError("the caller's own code failed")
    own.__context__ = context
    raised = ExceptionGroup("own", [own]) if grouped else own

    with pytest.raises(BodyError) as caught:
        await raises_in_the_body(gate, bearer, raised)

    assert caught.value.__context__ is context


@pytest.mark.parametrize("method", [READ, CALL], ids=["a read", "a tool call"])
async def test_a_request_whose_answer_names_no_request_ends_as_unreachable(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, method: str
) -> None:
    stands(app)
    gate.script(method, lambda reply: reply.raw_result({}, answering="no-request"))

    with pytest.raises(Unreachable):
        async with asyncio.timeout(LIMIT_SECONDS), opened(gate, bearer) as session:
            if method == READ:
                await session.read_standing()
            else:
                await session.invoke("a-tool", {})


async def test_an_answer_echoing_the_id_as_text_still_answers_its_request(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def echoes_the_id_as_text(reply: GateReply) -> None:
        contents = TextResourceContents(
            uri=STANDING_URI, text='{"standings": []}', mime_type="application/json"
        )
        await reply.result(
            ReadResourceResult(contents=[contents], ttl_ms=0, cache_scope="private"),
            answering=str(reply.rpc.id),
        )

    gate.script(READ, echoes_the_id_as_text)

    async with asyncio.timeout(LIMIT_SECONDS), opened(gate, bearer) as session:
        standing = await session.read_standing()

    assert standing.standings == ()


async def test_a_task_cancelled_during_a_member_call_ends_as_the_cancellation(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    calling = asyncio.Event()

    async def invokes() -> None:
        async with opened(gate, bearer) as session:
            await session.invoke("a-tool", {})

    gate.script(CALL, hangs_once_entered(calling))
    task = asyncio.create_task(invokes())

    ended = await cancelled_once_entered(task, calling)

    assert isinstance(ended, asyncio.CancelledError)


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaves(inner)]
    return [error]


# How a session ends: late answers, cancellation and a close that loses a child.


@pytest.fixture(
    params=[anyio.BrokenResourceError, anyio.ClosedResourceError],
    ids=["broken stream", "closed stream"],
)
def late_answer(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    def fails_as_it_closes(*, echoes: int = 1) -> type[Exception]:
        failing = transport_closing_with_late_answers(echo=request.param, echoes=echoes)
        monkeypatch.setattr("agents.mcp_client.session.streamable_http_client", failing)
        return request.param

    return fails_as_it_closes


async def test_an_answer_landing_after_the_close_is_unreachable_beside_the_failure_that_closed_it(
    gate: ScriptedGate, bearer: SecretStr, late_answer: Any
) -> None:
    late_answer()

    with pytest.raises(ExceptionGroup) as caught:
        await raises_in_the_body(gate, bearer, Unreachable("the stream outlasted its deadline"))

    assert [type(leaf) for leaf in _leaves(caught.value)] == [Unreachable, Unreachable]


async def test_an_answer_refused_before_the_close_does_not_outrank_the_failure_that_closed_it(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr, late_answer: Any
) -> None:
    late_answer()

    with pytest.raises(ExceptionGroup) as caught:
        await raises_after_a_refused_call(
            app, gate, bearer, Unreachable("the stream outlasted its deadline")
        )

    assert [type(leaf) for leaf in _leaves(caught.value)] == [Unreachable, Unreachable]


@pytest.mark.parametrize("echoes", [1, 2])
async def test_answers_landing_after_an_unrequested_cancellation_stay_the_errors_they_are(
    gate: ScriptedGate, bearer: SecretStr, late_answer: Any, echoes: int
) -> None:
    echo = late_answer(echoes=echoes)

    with pytest.raises(ExceptionGroup if echoes > 1 else echo) as caught:
        await raises_in_the_body(gate, bearer, asyncio.CancelledError())

    assert [type(leaf) for leaf in _leaves(caught.value)] == [echo] * echoes


async def cancelled_in_the_body(gate: ScriptedGate, bearer: SecretStr) -> BaseException | None:
    """Runs a session whose body waits and cancels its task there; what the task ended with."""
    inside = asyncio.Event()

    async def waits_in_the_body() -> None:
        async with opened(gate, bearer):
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(waits_in_the_body())
    async with asyncio.timeout(LIMIT_SECONDS):
        await inside.wait()
    task.cancel()
    async with asyncio.timeout(LIMIT_SECONDS):
        (ended,) = await asyncio.gather(task, return_exceptions=True)
    return ended


async def test_late_answers_alone_after_a_requested_cancellation_end_as_the_cancellation(
    gate: ScriptedGate, bearer: SecretStr, late_answer: Any
) -> None:
    late_answer(echoes=2)

    assert isinstance(await cancelled_in_the_body(gate, bearer), asyncio.CancelledError)


async def test_a_defect_beside_late_answers_after_a_cancellation_stays_with_them(
    gate: ScriptedGate, bearer: SecretStr, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "agents.mcp_client.session.streamable_http_client",
        transport_closing_with_leaves(ValueError("a defect"), anyio.BrokenResourceError()),
    )

    ended = await cancelled_in_the_body(gate, bearer)

    assert isinstance(ended, ExceptionGroup)
    assert [type(leaf) for leaf in _leaves(ended)] == [ValueError, Unreachable]


@pytest.mark.parametrize(
    "raised",
    [BodyError("the caller's own code failed"), StatusFailure(HTTPStatus.FORBIDDEN)],
    ids=["a defect", "a refusal"],
)
async def test_what_the_body_raises_stays_among_the_leaves_when_the_close_loses_a_child(
    gate: ScriptedGate, bearer: SecretStr, monkeypatch: pytest.MonkeyPatch, raised: Exception
) -> None:
    monkeypatch.setattr(
        "agents.mcp_client.session.streamable_http_client",
        transport_whose_close_loses_a_child(OSError),
    )

    with pytest.raises(ExceptionGroup) as caught:
        await raises_in_the_body(gate, bearer, raised)

    assert [leaf for leaf in _leaves(caught.value) if leaf is raised] == [raised]
    assert sorted(type(leaf).__name__ for leaf in _leaves(caught.value)) == sorted(
        [type(raised).__name__, "Unreachable"]
    )


async def test_a_task_cancelled_while_the_session_discovers_ends_as_the_cancellation(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    discovering = asyncio.Event()
    gate.script(DISCOVER, hangs_once_entered(discovering))
    task = asyncio.create_task(reads_the_standing(gate, bearer))

    ended = await cancelled_once_entered(task, discovering)

    assert isinstance(ended, asyncio.CancelledError)


async def test_a_task_cancelled_while_the_session_closes_ends_as_the_cancellation(
    gate: ScriptedGate, bearer: SecretStr, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold = ClosingHold()
    monkeypatch.setattr("agents.mcp_client.session.streamable_http_client", hold.transport())
    task = asyncio.create_task(reads_the_standing_without_reading(gate, bearer))

    ended = await cancelled_once_entered(task, hold.closing)

    assert isinstance(ended, asyncio.CancelledError)
    assert hold.cancelled.is_set()


async def reads_the_standing_without_reading(gate: ScriptedGate, bearer: SecretStr) -> None:
    async with opened(gate, bearer):
        pass
