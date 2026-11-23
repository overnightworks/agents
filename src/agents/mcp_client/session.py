"""A runner's session against an app's member endpoint: one SDK session over the shared HTTP policy.

The session writes no log line, decides nothing and holds no deployment fact: the URL, the bearer
and the transport come from the host. Every call that fails raises a ``CallFailure`` of
``agents.member_outcomes``.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final, Protocol, cast, override

import anyio
import httpx2
from logline import outgoing_headers
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CONNECTION_CLOSED,
    INVALID_PARAMS,
    REQUEST_TIMEOUT,
    UNSUPPORTED_PROTOCOL_VERSION,
    InputRequiredResult,
    JSONRPCError,
    ReadResourceResult,
    TextResourceContents,
    jsonrpc_message_adapter,
)
from pydantic import SecretStr, ValidationError

from agents.http_client import policy_client
from agents.member import (
    LISTEN_BOUND_SECONDS,
    STANDING_URI,
    MemberResourceRecord,
    MemberStanding,
    PlaceRef,
    PlaceView,
    PlaceWindow,
    member_resource_from_json,
    place_view_uri,
    place_window_uri,
)
from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    UNSUPPORTED_VERSION_CODE,
    CallFailure,
    StatusFailure,
    Unreachable,
    UnusableAnswer,
    jsonrpc_code,
    status_code,
)

CONNECT_SECONDS: Final = 30.0
READ_SECONDS: Final = 2 * LISTEN_BOUND_SECONDS
"""Twice the bound an app puts on a stream: the runner cuts a stream that outlasts it."""
_MEDIA_TYPE: Final = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
"""A media type as a log line may carry it: type and subtype of token characters, no parameters."""
_SDK_MEDIA_TYPES: Final = frozenset({"application/json", "text/event-stream"})
"""The media types of an answer the SDK reads a result from."""
_TRANSPORT_ERROR_CODES: Final = frozenset({CONNECTION_CLOSED, REQUEST_TIMEOUT})
"""What the SDK answers for a connection that broke or a request that timed out."""
_LATE_ANSWER_ERRORS: Final = (anyio.BrokenResourceError, anyio.ClosedResourceError)
"""What the transport's task raises when an answer lands after its session ended."""
_CALL_ERRORS: Final = (MCPError, ValidationError, httpx2.HTTPError, OSError)
"""The errors of the SDK and its transport that tell a call failed; any other error is a defect."""


class MemberSession(Protocol):
    """What a runner reads of an app; each read raises a ``StatusFailure``, ``Unreachable`` or
    ``UnusableAnswer``."""

    async def read_standing(self) -> MemberStanding:
        """Every standing of the member."""
        ...

    async def read_place(self, place: PlaceRef) -> PlaceWindow | None:
        """The place's window, or ``None`` when the app shows it to this member no more."""
        ...

    async def read_view(self, place: PlaceRef) -> PlaceView | None:
        """The place's view, or ``None`` when the app shows it to this member no more."""
        ...


@dataclass(frozen=True)
class _HttpAnswer:
    """What the app's last HTTP answer was: its status and, for an error status, the failed method
    and the answer."""

    status: int
    method: str | None = None
    answer: str | None = None


@dataclass
class _CallAnswer:
    """The last HTTP answer to one call, which only its own requests write, so no call beside it
    on the session can."""

    answer: _HttpAnswer | None = None


_CALL_ANSWER: Final = contextvars.ContextVar[_CallAnswer | None]("call_answer", default=None)
"""The answer of the call whose request this is; the SDK sends each request in the context of the
call that made it."""


class _BearerAuth(httpx2.Auth):
    """Adds the current bearer and the current trace to each request and keeps the last HTTP answer
    the app gave, to the session and to the call the request belongs to.

    The SDK turns an HTTP error into a bare protocol error, so this is where the status is seen.
    """

    def __init__(self, bearer: Callable[[], SecretStr]) -> None:
        self._bearer = bearer
        self.last_answer: _HttpAnswer | None = None

    @override
    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        request.headers["Authorization"] = f"Bearer {self._bearer().get_secret_value()}"
        request.headers.update(outgoing_headers())
        response = yield request
        if response.status_code < HTTPStatus.BAD_REQUEST:
            self.last_answer = _HttpAnswer(response.status_code)
        else:
            self.last_answer = _HttpAnswer(
                response.status_code, _method_of(request), await _answer_code_of(response)
            )
        if (call := _CALL_ANSWER.get()) is not None:
            call.answer = self.last_answer
        if response.status_code < HTTPStatus.BAD_REQUEST:
            _withhold_diagnostics(response)


def _withhold_diagnostics(response: httpx2.Response) -> None:
    """Hands the SDK no redirect to follow and no media type it would write into a log line.

    The SDK follows a redirect that stays on the app's origin whatever the client's policy says, and
    resends the request with its bearer; the policy is that none is followed. It also logs a
    content type it does not know at error level, in full, and the app chose that text. Only a
    media type it reads an answer from stays, so a redirect is no answer and an unknown media type
    names itself in no log line.
    """
    response.next_request = None
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if response.status_code >= HTTPStatus.MULTIPLE_CHOICES or media_type not in _SDK_MEDIA_TYPES:
        response.headers.pop("content-type", None)


def _method_of(request: httpx2.Request) -> str | None:
    """The MCP method the request carried; none for a request that carries no JSON-RPC message, as
    a reconnect does."""
    try:
        message = jsonrpc_message_adapter.validate_json(request.content, by_name=False)
    except ValidationError:
        return None
    return getattr(message, "method", None)


async def _answer_code_of(response: httpx2.Response) -> str | None:
    """The code of the JSON-RPC error an error status carries, else the media type of the answer,
    never its content.

    The SDK takes an error body for a JSON-RPC error when the answer says it is JSON and the body
    parses as one.
    """
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type == "application/json":
        try:
            await response.aread()
            message = jsonrpc_message_adapter.validate_json(response.content, by_name=False)
        except httpx2.DecodingError, ValidationError:
            pass
        else:
            if isinstance(message, JSONRPCError):
                return jsonrpc_code(message.error.code)
    return media_type if _MEDIA_TYPE.fullmatch(media_type) else None


class _UnknownResource(Exception):  # noqa: N818
    """The app shows this member no such resource."""


@dataclass
class _McpMemberSession:
    session: ClientSession
    auth: _BearerAuth = field(repr=False)

    async def read_standing(self) -> MemberStanding:
        return _record_of(MemberStanding, await self._asked(self._read(STANDING_URI)))

    async def read_place(self, place: PlaceRef) -> PlaceWindow | None:
        return await self._read_record(PlaceWindow, place_window_uri(place))

    async def read_view(self, place: PlaceRef) -> PlaceView | None:
        return await self._read_record(PlaceView, place_view_uri(place))

    async def _read_record[Record: MemberResourceRecord](
        self, record: type[Record], uri: str
    ) -> Record | None:
        """The record at the resource, or ``None`` when the app shows this member no such one."""
        read = self._read(uri, unknown_is_absent=True)
        try:
            return _record_of(record, await self._asked(read))
        except _UnknownResource:
            return None

    async def _read(self, uri: str, *, unknown_is_absent: bool = False) -> ReadResourceResult:
        """The resource; an app that shows this member no such one answers an invalid-params
        error."""
        try:
            result = await self.session.read_resource(uri, allow_input_required=True)
        except MCPError as error:
            if unknown_is_absent and error.code == INVALID_PARAMS:
                raise _UnknownResource from None
            raise
        if isinstance(result, InputRequiredResult):
            raise UnusableAnswer(INVALID_ANSWER_CODE)
        return result

    async def _asked[Answer](self, request: Awaitable[Answer]) -> Answer:
        """The call's answer; a failure is told by the call's own last HTTP answer, never by one to
        a call beside it."""
        self.auth.last_answer = None
        call = _CallAnswer()
        reset = _CALL_ANSWER.set(call)
        try:
            return await request
        except _CALL_ERRORS as error:
            raise _failure_of(error, call.answer) from None
        finally:
            _CALL_ANSWER.reset(reset)


def _record_of[Record: MemberResourceRecord](
    record: type[Record], result: ReadResourceResult
) -> Record:
    text = "".join(
        block.text for block in result.contents if isinstance(block, TextResourceContents)
    )
    try:
        return member_resource_from_json(record, text)
    except ValidationError:
        raise UnusableAnswer(INVALID_ANSWER_CODE) from None


def _failure_of(error: BaseException, answered: _HttpAnswer | None) -> CallFailure:
    """A call that gave no usable answer: told by the last HTTP answer it was given, else by the
    error.

    A protocol error or a result the SDK cannot validate is an unusable answer; only a broken or
    silent connection is unreachable. The error is one of ``_CALL_ERRORS``: the status is read only
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
    if isinstance(error, ValidationError):
        return UnusableAnswer(INVALID_ANSWER_CODE)
    return Unreachable("the app did not answer")


def _settled(
    leaf: BaseException, from_body: set[int], auth: _BearerAuth, *, beside_no_echo: bool
) -> BaseException:
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
    if isinstance(leaf, _CALL_ERRORS):
        return _failure_of(leaf, auth.last_answer)
    if beside_no_echo and isinstance(leaf, _LATE_ANSWER_ERRORS):
        return Unreachable("an answer landed after the session closed")
    return leaf


def _leaves_of(error: BaseException) -> Iterator[BaseException]:
    """The errors an exception group holds, however deep; any other error is its own leaf."""
    if isinstance(error, BaseExceptionGroup):
        for inner in cast("BaseExceptionGroup[BaseException]", error).exceptions:
            yield from _leaves_of(inner)
    else:
        yield error


def _task_is_being_cancelled() -> bool:
    """Whether the running task has a cancellation requested that it has not yet taken."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _folded(
    error: Exception,
    raised_in_body: Exception | None,
    auth: _BearerAuth,
    *,
    cancelled_in_body: bool,
) -> BaseException:
    """What a session that ended in ``error`` raises.

    The SDK's task groups wrap what is raised inside them, so every leaf is looked at: a failure of
    the call and what the caller's own code raised stay as they were, an error of the connection
    becomes its failure and a defect stays a defect. The runner picks what decides. A close that
    failed or was cut can replace what the body raised; the body's leaves stay, ahead of what the
    close met, so that of equal ranks a call's own failure tells how the session failed, not an
    answer it cut.
    """
    from_body = list(_leaves_of(raised_in_body)) if raised_in_body else []
    found = [
        *from_body,
        *(leaf for leaf in _leaves_of(error) if all(leaf is not own for own in from_body)),
    ]
    if cancelled_in_body and all(isinstance(leaf, _LATE_ANSWER_ERRORS) for leaf in found):
        return asyncio.CancelledError()
    beside_no_echo = any(not isinstance(leaf, _LATE_ANSWER_ERRORS) for leaf in found)
    from_body_ids = {id(own) for own in from_body}
    leaves = [_settled(leaf, from_body_ids, auth, beside_no_echo=beside_no_echo) for leaf in found]
    if len(leaves) == 1:
        return leaves[0]
    return ExceptionGroup("the session failed", cast("list[Exception]", leaves))


@asynccontextmanager
async def open_member_session(
    url: str,
    bearer: Callable[[], SecretStr],
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    stream_seconds: float = READ_SECONDS,
) -> AsyncGenerator[MemberSession]:
    """A session at the app's revision; the failures a call raises are the ones ``MemberSession``
    names.

    ``bearer`` is read for every request, so a renewed token reaches the next one. A bare
    ``ClientSession`` discovers the revision: the SDK's own client would fall back to ``initialize``
    on any error, so a 401 on the probe would enter a path this runner does not have. Several
    leaves, failures or defects, leave as one ``ExceptionGroup`` holding each of them. A session
    whose task was being cancelled while it discovered the revision or when the body left, and that
    closes with late answers alone, raises the cancellation: the SDK's task groups lower the task's
    cancellation count and turn it into those answers.
    """
    auth = _BearerAuth(bearer)
    timeout = httpx2.Timeout(CONNECT_SECONDS, read=stream_seconds)
    raised_in_body: Exception | None = None
    cancelled_in_body = False
    try:
        async with (
            policy_client(timeout=timeout, transport=transport, auth=auth) as client,
            streamable_http_client(url, http_client=client) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            try:
                await _discover(session, auth)
            except asyncio.CancelledError:
                cancelled_in_body = _task_is_being_cancelled()
                raise
            try:
                yield _McpMemberSession(session, auth)
            except Exception as error:
                raised_in_body = error
                raise
            except asyncio.CancelledError:
                cancelled_in_body = _task_is_being_cancelled()
                raise
    except Exception as error:
        raise _folded(error, raised_in_body, auth, cancelled_in_body=cancelled_in_body) from None


async def _discover(session: ClientSession, auth: _BearerAuth) -> None:
    auth.last_answer = None
    try:
        await session.discover()
    except RuntimeError:
        raise UnusableAnswer(UNSUPPORTED_VERSION_CODE) from None
    except _CALL_ERRORS as error:
        raise _failure_of(error, auth.last_answer) from None
