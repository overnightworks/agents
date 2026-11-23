"""How a member call's errors become its failure, and how a session's leaves become what it
raises.

The SDK's task groups wrap what is raised inside them, so every leaf of a session's error is
looked at: a failure of the call and what the caller's own code raised stay as they were, an error
of the connection becomes its failure and a defect stays a defect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from http import HTTPStatus
from typing import Final, cast

import anyio
import httpx2
from mcp.client.subscriptions import SubscriptionLost
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT, UNSUPPORTED_PROTOCOL_VERSION
from pydantic import ValidationError

from agents.mcp_client.exchange import BearerAuth, HttpAnswer
from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    CallFailure,
    StatusFailure,
    Unreachable,
    UnusableAnswer,
    jsonrpc_code,
    status_code,
)

_TRANSPORT_ERROR_CODES: Final = frozenset({CONNECTION_CLOSED, REQUEST_TIMEOUT})
"""What the SDK answers for a connection that broke or a request that timed out."""
LATE_ANSWER_ERRORS: Final = (anyio.BrokenResourceError, anyio.ClosedResourceError)
"""What the transport's task raises when an answer lands after its session ended."""


class InvalidToolResult(Exception):  # noqa: N818
    """The app's result does not meet the output schema it advertised; carries none of its
    content."""


CALL_ERRORS: Final = (
    MCPError,
    SubscriptionLost,
    ValidationError,
    InvalidToolResult,
    httpx2.HTTPError,
    OSError,
)
"""The errors of the SDK and its transport that tell a call failed; any other error is a defect."""


def failure_of(error: BaseException, answered: HttpAnswer | None) -> CallFailure:
    """A call that gave no usable answer: told by the last HTTP answer it was given, else by the
    error.

    A protocol error or a result the SDK cannot validate is an unusable answer; only a broken or
    silent connection is unreachable. The error is one of ``CALL_ERRORS``: the status is read only
    for those, never for a late answer's error, which no call raised.
    """
    if (
        isinstance(error, MCPError)
        and error.code == UNSUPPORTED_PROTOCOL_VERSION
        and answered is not None
        and answered.status >= HTTPStatus.BAD_REQUEST
    ):
        code = jsonrpc_code(error.code)
        return UnusableAnswer(code, answered.method, code)
    if answered is not None and answered.status >= HTTPStatus.BAD_REQUEST:
        return StatusFailure(answered.status, answered.method, answered.answer)
    if answered is not None and answered.status >= HTTPStatus.MULTIPLE_CHOICES:
        return UnusableAnswer(status_code(answered.status))
    if isinstance(error, MCPError):
        if error.code in _TRANSPORT_ERROR_CODES:
            return Unreachable("the app did not answer")
        return UnusableAnswer(jsonrpc_code(error.code))
    if isinstance(error, SubscriptionLost):
        return Unreachable("the stream was lost")
    if isinstance(error, ValidationError | InvalidToolResult):
        return UnusableAnswer(INVALID_ANSWER_CODE)
    return Unreachable("the app did not answer")


def _settled(
    leaf: Exception, from_body: set[int], auth: BearerAuth, *, beside_no_echo: bool
) -> Exception:
    """A failure or a leaf the caller's body raised stays as it was; an error of a call becomes its
    failure.

    The transport's task finds its read stream closed when an answer lands after the session ended:
    beside a leaf that is no such error that is a lost answer, unreachable whatever status it
    carried. Alone it is how a cancelled session unwinds, and it stays as it was so that no
    cancellation turns into a failure the runner backs off from; the session itself turns it into
    the cancellation where its task was cancelled.
    """
    if isinstance(leaf, CallFailure) or id(leaf) in from_body:
        return leaf
    if isinstance(leaf, CALL_ERRORS):
        return failure_of(leaf, auth.last_answer)
    if beside_no_echo and isinstance(leaf, LATE_ANSWER_ERRORS):
        return Unreachable("an answer landed after the session closed")
    return leaf


def leaves_of(error: Exception) -> Iterator[Exception]:
    """The errors an exception group holds, however deep; any other error is its own leaf."""
    if isinstance(error, ExceptionGroup):
        for inner in cast("ExceptionGroup[Exception]", error).exceptions:
            yield from leaves_of(inner)
    else:
        yield error


def task_is_being_cancelled() -> bool:
    """Whether the running task has a cancellation requested that it has not yet taken."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def folded(
    error: Exception,
    raised_in_body: Exception | None,
    auth: BearerAuth,
    *,
    cancelled_in_body: bool,
) -> Exception | asyncio.CancelledError:
    """What a session that ended in ``error`` raises.

    A close that failed or was cut can replace what the body raised; the body's leaves stay, ahead
    of what the close met, so that of equal ranks a call's own failure tells how the session
    failed, not an answer it cut.
    """
    from_body = list(leaves_of(raised_in_body)) if raised_in_body else []
    found = [
        *from_body,
        *(leaf for leaf in leaves_of(error) if all(leaf is not own for own in from_body)),
    ]
    if cancelled_in_body and all(isinstance(leaf, LATE_ANSWER_ERRORS) for leaf in found):
        return asyncio.CancelledError()
    beside_no_echo = any(not isinstance(leaf, LATE_ANSWER_ERRORS) for leaf in found)
    from_body_ids = {id(own) for own in from_body}
    leaves = [_settled(leaf, from_body_ids, auth, beside_no_echo=beside_no_echo) for leaf in found]
    if len(leaves) == 1:
        return leaves[0]
    return ExceptionGroup("the session failed", leaves)
