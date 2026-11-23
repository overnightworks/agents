"""How a member session ends: what the caller's body raises, late answers, cancellation and a close
that loses a child."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any

import anyio
import pytest
from pydantic import SecretStr, ValidationError

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
    opened,
    reads_the_standing,
    stands,
)
from tests.support.scripted_gate import (
    DISCOVER,
    GateReply,
    ScriptedGate,
    hangs,
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
    with pytest.raises(type(own)) as caught:
        await raises_after_a_refused_call(app, gate, bearer, ExceptionGroup("own", [own]))

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

    with pytest.raises(BaseException) as caught:  # noqa: PT011 - one error or a group of them
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

    async def hangs_in_discovery(reply: GateReply) -> None:
        discovering.set()
        await hangs(reply)

    gate.script(DISCOVER, hangs_in_discovery)
    task = asyncio.create_task(reads_the_standing(gate, bearer))
    async with asyncio.timeout(LIMIT_SECONDS):
        await discovering.wait()
    task.cancel()

    async with asyncio.timeout(LIMIT_SECONDS):
        (ended,) = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(ended, asyncio.CancelledError)


async def test_a_task_cancelled_while_the_session_closes_ends_as_the_cancellation(
    gate: ScriptedGate, bearer: SecretStr, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold = ClosingHold()
    monkeypatch.setattr("agents.mcp_client.session.streamable_http_client", hold.transport())
    task = asyncio.create_task(reads_the_standing_without_reading(gate, bearer))
    async with asyncio.timeout(LIMIT_SECONDS):
        await hold.closing.wait()
    task.cancel()

    async with asyncio.timeout(LIMIT_SECONDS):
        (ended,) = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(ended, asyncio.CancelledError)
    assert hold.cancelled.is_set()


async def reads_the_standing_without_reading(gate: ScriptedGate, bearer: SecretStr) -> None:
    async with opened(gate, bearer):
        pass
