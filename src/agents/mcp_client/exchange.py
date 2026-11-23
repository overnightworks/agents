"""What the member session sees of its HTTP exchange: the bearer each request carries, the last
answer of the app and the answer of each call.

The SDK turns an HTTP error into a bare protocol error, so the auth flow is where the status
is seen.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final, override

import httpx2
from logline import outgoing_headers
from mcp.shared.dispatcher import coerce_request_id
from mcp_types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCRequest,
    JSONRPCResponse,
    jsonrpc_message_adapter,
)
from pydantic import SecretStr, ValidationError

from agents.member_outcomes import jsonrpc_code

_MEDIA_TYPE: Final = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")
"""A media type as a log line may carry it: type and subtype of token characters, no parameters."""
_JSON_MEDIA_TYPE: Final = "application/json"
_SDK_MEDIA_TYPES: Final = frozenset({_JSON_MEDIA_TYPE, "text/event-stream"})
"""The media types of an answer the SDK reads a result from."""


@dataclass(frozen=True)
class HttpAnswer:
    """What the app's last HTTP answer was: its status and, for an error status, the failed method
    and the answer."""

    status: int
    method: str | None = None
    answer: str | None = None


@dataclass
class CallAnswer:
    """The last HTTP answer to one call, which only its own requests write, so no call beside it
    on the session can; and the deadline that ends the call when its answer names another
    request."""

    answer: HttpAnswer | None = None
    stranding: asyncio.Timeout | None = None

    def strand(self) -> None:
        """Ends the call now: the app answered, and the answer is for no request of the call."""
        if self.stranding is not None:
            self.stranding.reschedule(asyncio.get_running_loop().time())


CALL_ANSWER: Final = contextvars.ContextVar[CallAnswer | None]("call_answer", default=None)
"""The answer of the call whose request this is; the SDK sends each request in the context of the
call that made it."""


class BearerAuth(httpx2.Auth):
    """Adds the current bearer and the current trace to each request and keeps the last HTTP answer
    the app gave, to the session and to the call the request belongs to.

    The SDK turns an HTTP error into a bare protocol error, so this is where the status is seen.
    """

    def __init__(self, bearer: Callable[[], SecretStr]) -> None:
        self._bearer = bearer
        self.last_answer: HttpAnswer | None = None

    def read_bearer(self) -> str:
        """The current bearer; one that is no header value is refused here.

        The SDK's transport tasks end the whole session when a request fails in them, so a call
        reads the bearer before it makes its request and fails alone.
        """
        token = self._bearer().get_secret_value()
        if not _is_header_value(token):
            # Raised before the header exists, so no library error holds the token.
            raise ValueError("the bearer is no valid header value")
        return token

    @override
    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = self.read_bearer()
        request.headers["Authorization"] = f"Bearer {token}"
        request.headers.update(outgoing_headers())
        response = yield request
        if response.status_code < HTTPStatus.BAD_REQUEST:
            self.last_answer = HttpAnswer(response.status_code)
        else:
            self.last_answer = HttpAnswer(
                response.status_code, _method_of(request), await _answer_code_of(response)
            )
        if (call := CALL_ANSWER.get()) is not None:
            call.answer = self.last_answer
        if response.status_code < HTTPStatus.BAD_REQUEST:
            await _strand_an_answer_to_no_request(request, response)
            _withhold_diagnostics(response)


def _is_header_value(token: str) -> bool:
    """Whether the token can be the rest of an ``Authorization`` header: not empty, no control
    character (newline included) and no trailing space, which the HTTP library refuses by naming the
    whole value."""
    return token.isascii() and token.isprintable() and bool(token) and not token.endswith(" ")


async def _strand_an_answer_to_no_request(
    request: httpx2.Request, response: httpx2.Response
) -> None:
    """Ends the call whose complete JSON answer is the answer to no request of its own.

    The SDK drops an answer whose id matches no request, and the request it was sent for waits
    for ever; the id is the only thing that tells such an answer from the call's own, and the SDK
    decides whether two ids are one (``coerce_request_id``), so this does the same.
    """
    if (call := CALL_ANSWER.get()) is None or _media_type_of(response) != _JSON_MEDIA_TYPE:
        return
    asked = _message_of(request)
    if not isinstance(asked, JSONRPCRequest):
        return
    await response.aread()
    try:
        answer = jsonrpc_message_adapter.validate_json(response.content, by_name=False)
    except ValidationError:
        return
    if isinstance(answer, JSONRPCResponse | JSONRPCError) and (
        answer.id is None or coerce_request_id(answer.id) != coerce_request_id(asked.id)
    ):
        call.strand()


def _withhold_diagnostics(response: httpx2.Response) -> None:
    """Hands the SDK no redirect to follow and no media type it would write into a log line.

    The SDK follows a redirect that stays on the app's origin whatever the client's policy says, and
    resends the request with its bearer; the policy is that none is followed. It also logs a
    content type it does not know at error level, in full, and the app chose that text. Only a
    media type it reads an answer from stays, so a redirect is no answer and an unknown media type
    names itself in no log line.
    """
    response.next_request = None
    media_type = _media_type_of(response)
    if response.status_code >= HTTPStatus.MULTIPLE_CHOICES or media_type not in _SDK_MEDIA_TYPES:
        response.headers.pop("content-type", None)


def _message_of(request: httpx2.Request) -> JSONRPCMessage | None:
    """The JSON-RPC message the request carried; none for a request that carries none, as a
    reconnect does."""
    try:
        return jsonrpc_message_adapter.validate_json(request.content, by_name=False)
    except ValidationError:
        return None


def _method_of(request: httpx2.Request) -> str | None:
    """The MCP method the request carried."""
    return getattr(_message_of(request), "method", None)


def _media_type_of(response: httpx2.Response) -> str:
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


async def _answer_code_of(response: httpx2.Response) -> str | None:
    """The code of the JSON-RPC error an error status carries, else the media type of the answer,
    never its content.

    The SDK takes an error body for a JSON-RPC error when the answer says it is JSON and the body
    parses as one.
    """
    media_type = _media_type_of(response)
    if media_type == _JSON_MEDIA_TYPE:
        try:
            await response.aread()
            message = jsonrpc_message_adapter.validate_json(response.content, by_name=False)
        except httpx2.DecodingError, ValidationError:
            pass
        else:
            if isinstance(message, JSONRPCError):
                return jsonrpc_code(message.error.code)
    return media_type if _MEDIA_TYPE.fullmatch(media_type) else None
