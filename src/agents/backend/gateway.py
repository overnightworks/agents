"""The gateway as a backend: one streamed completion per round of a turn, or one picture.

A turn runs under one deadline.

The app's key never leaves this module: it is read per turn, sent as the bearer
and written to no exception or log record.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import logging
import re
import ssl
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from http import HTTPStatus
from pathlib import Path
from types import TracebackType
from typing import Final

import httpx2
from logline import StepTimer, outgoing_headers
from pydantic import JsonValue, TypeAdapter

from agents.agent_file import TurnRequest
from agents.backend import (
    MAX_RETRY_AFTER_S,
    AgentBackend,
    TurnEvent,
    TurnPerson,
    TurnStopKind,
    TurnStopped,
)
from agents.backend.gateway_request import (
    MAX_REQUEST_BYTES,
    bounded,
    opening_messages,
    picture_body,
    request_body,
    round_messages,
)
from agents.backend.streamed_round import (
    AssembledMessage,
    MalformedRound,
    RoundErrorEvent,
    StreamedRound,
    checked_error_of,
    read_round,
)
from agents.backend.tool_result import RequestedCall, serve
from agents.config import AgentConfig, ChatGateway
from agents.tools import (
    HandTool,
    JsonObject,
    ToolDeclaration,
)
from agents.turn import (
    MAX_EVENT_BYTES,
    MAX_RUN_FILES_BYTES,
    FailureCode,
    NonEmptyUnicodeText,
    RejectionCode,
    RoundCall,
    RoundUsage,
    RunFile,
    ToolRound,
    TurnUsage,
    base64_length,
    loaded_json,
    person_fingerprint,
)
from agents.turn_events import (
    AssistantTextEvent,
    FinalEvent,
    ToolCallEvent,
    ToolResultEvent,
)

_LOGGER: Final = logging.getLogger(__name__)
_COMPLETIONS_PATH: Final = "/v1/chat/completions"
_PICTURES_PATH: Final = "/v1/images/generations"
_MODEL_REQUEST_EVENT: Final = "model_request.ended"
_PICTURE_REQUEST_EVENT: Final = "picture_request.ended"
_BUDGET_EXCEEDED: Final = "budget_exceeded"
_TRAILING_READ_SECONDS: Final = 1
_TRAILING_READ_BYTES: Final = 65536
_PICTURE_FILE_NAME: Final = "picture"
_MAX_KEY_BYTES: Final = 1024
_PRINTABLE_ASCII: Final = re.compile(r"[\x21-\x7e]+")
_MAX_PICTURE_BODY_BYTES: Final = base64_length(MAX_RUN_FILES_BYTES) + MAX_EVENT_BYTES
_UNREADABLE_REPLY: Final = (
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    AttributeError,
    RecursionError,
)
_ARGUMENTS: Final = TypeAdapter(JsonObject)
_CALL_ID: Final = TypeAdapter(NonEmptyUnicodeText)
_ROUND_TEXT: Final = TypeAdapter(NonEmptyUnicodeText | None)
_NOT_RUN_CONTENT: Final = "not run: one call per reply"


class GatewayDetail(StrEnum):
    """Why a request stopped: the reason of its line."""

    KEY_UNUSABLE = "key_unusable"
    GATEWAY_REFUSED = "gateway_refused"
    GATEWAY_FAILED = "gateway_failed"
    CONNECTION_FAILED = "connection_failed"
    MALFORMED_REPLY = "malformed_reply"


class GatewayBackend(AgentBackend):
    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: Iterable[HandTool] = (),
        transport: httpx2.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._now = _utc_now if now is None else now
        self._transport = transport
        self._pooled: httpx2.AsyncClient | None = None
        self._closed = False
        self._tools: dict[str, HandTool] = {}
        for tool in tools:
            if tool.declaration.name in self._tools:
                raise ValueError(f"tool {tool.declaration.name!r} is given twice")
            self._tools[tool.declaration.name] = tool

    async def __aenter__(self) -> GatewayBackend:
        return self

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the pooled connections; a ``run`` or a round after this raises ``RuntimeError``.

        The pooled client is bound to the event loop of its first request: drive one backend
        from one loop.
        """
        self._closed = True
        if self._pooled is not None:
            pooled, self._pooled = self._pooled, None
            await pooled.aclose()

    async def run(
        self, request: TurnRequest, person: TurnPerson
    ) -> AsyncGenerator[TurnEvent, None]:
        """The turn's events; a stop leaves this frame holding neither the key nor a reply.

        A stop the gateway path raised is replaced by a plain one, with nothing of the frames or
        errors it passed through; a stop the host raised is the same exception, its traceback
        cut of the gateway path's frames, which hold the key, the reply and the call arguments.
        """
        if self._closed:
            raise RuntimeError("the gateway backend is closed")
        try:
            async with contextlib.aclosing(self._turn(request, person)) as events:
                async for event in events:
                    try:
                        yield event
                    finally:
                        # The next pull or the host's close can stop the turn; this frame then
                        # holds no text piece.
                        del event
        except _GatewayStop as stopped:
            closed = _plain(stopped)
        except TurnStopped as stopped:
            raise stopped.with_traceback(_beyond_the_gateway_path(stopped.__traceback__))
        else:
            return
        raise closed

    async def _turn(
        self, request: TurnRequest, person: TurnPerson
    ) -> AsyncGenerator[TurnEvent, None]:
        request = TurnRequest.model_validate(request)
        tools = self._declared_tools(request)
        if person.role not in request.roles:
            raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.FORBIDDEN)
        deadline = asyncio.get_running_loop().time() + request.max_duration_seconds
        if request.picture_size is not None:
            yield await self._picture(request, request.picture_size, person, deadline)
            return
        progress = _Progress(
            request.max_tool_calls or 0,
            call_ids={call.call_id for each in request.turn.rounds for call in each.calls},
        )
        key = await _before(deadline, self._authorise(person))
        offered, loadable = frozenset(tools), request.deferred_tools
        messages = opening_messages(request)
        user = person_fingerprint(person.principal)
        rounds: list[RoundUsage] = []
        tool_rounds: list[ToolRound] = []
        while True:
            _refuse_after(deadline)
            body = bounded(request_body(request, messages, user), _limit_exceeded)
            opened = partial(self._post, _COMPLETIONS_PATH, body, key)
            started_at = self._now()
            streamed = _streamed_round(
                opened, offered, progress.call_ids, deadline, request.one_call_per_reply
            )
            async with contextlib.aclosing(streamed) as parts:
                while isinstance(part := await anext(parts), str):
                    yield AssistantTextEvent(text=part)
                reply = part
            rounds.append(RoundUsage(started_at=started_at, usage=reply.usage))
            if not reply.calls:
                yield FinalEvent(
                    text=reply.text,
                    usage=_summed(rounds),
                    rounds=tuple(rounds),
                    tool_rounds=tuple(tool_rounds),
                )
                return
            progress.admit(reply.calls, reply.served)
            results: list[ToolResultEvent] = []
            for call in reply.served:
                yield ToolCallEvent(tool_use_id=call.id, name=call.name, input=call.arguments)
                _refuse_after(deadline)
                handler = self._tools[call.name].handler
                result = await _before(deadline, serve(handler, person, call, loadable))
                results.append(result)
                yield result
            tool_rounds.append(_tool_round(reply, results))
            messages += round_messages(tool_rounds[-1])

    def _declared_tools(self, request: TurnRequest) -> dict[str, ToolDeclaration]:
        tools = {declaration.name: declaration for declaration in request.tools}
        for name, declaration in tools.items():
            if name not in self._tools:
                raise ValueError(f"tool {name!r} has no handler")
            if self._tools[name].declaration != declaration:
                raise ValueError(f"tool {name!r} is declared differently than its handler")
        return tools

    async def _authorise(self, person: TurnPerson) -> str:
        if self._config.api_group not in await person.groups():
            raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.NOT_ENABLED)
        return _read_key(self._config.gateway.key_file)

    async def _picture(
        self, request: TurnRequest, size: str, person: TurnPerson, deadline: float
    ) -> FinalEvent:
        key = await _before(deadline, self._authorise(person))
        _refuse_after(deadline)
        body = bounded(
            picture_body(request, size, person_fingerprint(person.principal)), _limit_exceeded
        )
        async with _request_line(_PICTURE_REQUEST_EVENT) as step:
            file = await _before(deadline, self._generated(body, key, step))
        return FinalEvent(text="", files=(file,))

    async def _generated(self, body: str, key: str, step: StepTimer) -> RunFile:
        async with self._post(_PICTURES_PATH, body, key, step) as response:
            return await _picture_of(response)

    def _post(
        self, path: str, body: str, key: str, step: StepTimer
    ) -> contextlib.AbstractAsyncContextManager[httpx2.Response]:
        if self._closed:
            raise RuntimeError("the gateway backend is closed")
        if self._pooled is None:
            self._pooled = _client(self._config.gateway, self._transport)
        return _opened(self._pooled, path, key, step, content=body)


@contextlib.asynccontextmanager
async def _opened(
    client: httpx2.AsyncClient, path: str, key: str, step: StepTimer, *, content: str
) -> AsyncGenerator[httpx2.Response, None]:
    """The gateway's 200 response, open while it is read; a connection lost meanwhile is raised.

    The request carries the host's ``traceparent`` inside a trace and none outside one, and the
    response's status goes on the request's line, and so does ``connect_ms`` when the request
    opened a new connection, whether or not a response came.
    """
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        **outgoing_headers(),
    }
    connecting = _ConnectTimes()
    try:
        # ssl.SSLError is an OSError.
        async with client.stream(
            "POST",
            path,
            content=content,
            headers=headers,
            extensions={"trace": connecting.note},
        ) as response:
            step.add(status=response.status_code)
            await _raise_unless_ok(response)
            yield response
    except httpx2.HTTPError, httpx2.InvalidURL, OSError:
        raise _unavailable(GatewayDetail.CONNECTION_FAILED) from None
    finally:
        if (connect_ms := connecting.milliseconds()) is not None:
            step.add(connect_ms=connect_ms)


class _ConnectTimes:
    """When a request's new connection started to connect and finished, by the library's events.

    The library fires these events only for a connection without a stream. The event's ``info``
    carries the request with the bearer header, so it is never read.
    """

    _STARTED: Final = "connection.connect_tcp.started"
    _TCP_DONE: Final = "connection.connect_tcp.complete"
    _TLS_STARTED: Final = "connection.start_tls.started"
    _TLS_DONE: Final = "connection.start_tls.complete"

    def __init__(self) -> None:
        self._at: dict[str, float] = {}

    def note(self, event: str, info: Mapping[str, object]) -> Coroutine[None, None, None]:
        # The library awaits the callback and rejects anything but a coroutine.
        del info
        self._at.setdefault(event, time.perf_counter())
        return asyncio.sleep(0)

    def milliseconds(self) -> float | None:
        """The connect, or its TLS handshake, in milliseconds; none when it did not complete."""
        started = self._at.get(self._STARTED)
        if self._TLS_STARTED in self._at:
            done = self._at.get(self._TLS_DONE)
        else:
            done = self._at.get(self._TCP_DONE)
        if started is None or done is None:
            return None
        return round((done - started) * 1000, 3)


def _client(
    gateway: ChatGateway, transport: httpx2.AsyncBaseTransport | None
) -> httpx2.AsyncClient:
    try:
        verify = True
        if gateway.ca_file is not None and transport is None:
            verify = ssl.create_default_context(cafile=str(gateway.ca_file))
        # The jar allows no domain, so a pooled client stores and sends no cookie across
        # requests; the deadline is the only timeout.
        return httpx2.AsyncClient(
            base_url=gateway.origin,
            transport=transport,
            verify=verify,
            trust_env=False,
            follow_redirects=False,
            timeout=None,
            cookies=http.cookiejar.CookieJar(
                policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
            ),
        )
    except httpx2.InvalidURL, OSError:  # ssl.SSLError is an OSError.
        raise _unavailable(GatewayDetail.CONNECTION_FAILED) from None


@dataclass(frozen=True)
class _Round:
    """One judged reply: the answer when the model stopped, else the calls it asked for.

    ``text`` is the answer, or the text that came with the calls. ``served`` is the prefix of
    ``calls`` the turn runs; the rest reach the model as not run. ``calls_not_run`` counts them
    when one call per reply split the round, else it is None.
    """

    text: str
    calls: tuple[RequestedCall, ...]
    served: tuple[RequestedCall, ...] = ()
    calls_not_run: int | None = None
    usage: TurnUsage | None = None


@dataclass
class _Progress:
    """What a turn has done so far, for the bounds on it."""

    max_tool_calls: int
    calls: int = 0
    call_ids: set[str] = field(default_factory=set[str])

    def admit(self, calls: tuple[RequestedCall, ...], served: tuple[RequestedCall, ...]) -> None:
        if self.calls + len(served) > self.max_tool_calls:
            raise _limit_exceeded()
        self.calls += len(served)
        self.call_ids |= {call.id for call in calls}


async def _before[Result](deadline: float, step: Awaitable[Result]) -> Result:
    """Await a step of the turn; the turn's one deadline cancels it."""
    scope = asyncio.timeout_at(deadline)
    try:
        async with scope:
            return await step
    except TimeoutError:
        if not scope.expired():
            raise
        raise _limit_exceeded() from None


def _refuse_after(deadline: float) -> None:
    """A step the turn has not started is not started past its deadline."""
    if asyncio.get_running_loop().time() >= deadline:
        raise _limit_exceeded()


def _limit_exceeded() -> TurnStopped:
    return _GatewayStop(TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)


def _not_run(call: RequestedCall) -> ToolResultEvent:
    return ToolResultEvent(tool_use_id=call.id, content=_NOT_RUN_CONTENT, is_error=True)


def _tool_round(reply: _Round, results: list[ToolResultEvent]) -> ToolRound:
    """The round whole: each call with the answer the model is shown, the not run ones included."""
    answers = [*results, *(_not_run(call) for call in reply.calls[len(reply.served) :])]
    return ToolRound(
        text=reply.text or None,
        calls=tuple(
            RoundCall(
                call_id=call.id,
                tool=call.name,
                arguments=call.arguments,
                result=answer.content,
                is_error=answer.is_error,
                image=answer.image,
                loads=answer.loads,
            )
            for call, answer in zip(reply.calls, answers, strict=True)
        ),
    )


def _summed(rounds: list[RoundUsage]) -> TurnUsage | None:
    """The turn's usage: its rounds' summed, or ``None`` when any round has none."""
    total: TurnUsage | None = None
    for each in rounds:
        if each.usage is None:
            return None
        total = each.usage if total is None else total + each.usage
    return total


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _read_key(key_file: Path) -> str:
    try:
        with key_file.open("rb") as file:
            raw = file.read(_MAX_KEY_BYTES + 1)
    except OSError:
        raise _unavailable(GatewayDetail.KEY_UNUSABLE) from None
    line = raw.decode("ascii", errors="replace").removesuffix("\n")
    if len(raw) > _MAX_KEY_BYTES or _PRINTABLE_ASCII.fullmatch(line) is None:
        raise _unavailable(GatewayDetail.KEY_UNUSABLE)
    return line


class _GatewayStop(TurnStopped):
    """A stop this module's own code raises, with the context and frames of where it arose."""


def _plain(stopped: TurnStopped) -> TurnStopped:
    return TurnStopped(
        stopped.kind,
        stopped.code,
        retry_after_s=stopped.retry_after_s,
        detail=stopped.detail,
    )


def _beyond_the_gateway_path(trace: TracebackType | None) -> TracebackType | None:
    """The traceback without the gateway path's frames: they hold the key, the reply and the calls.

    The path is this module, ``gateway_request`` and ``tool_result``, whose ``serve`` frame holds
    the call's arguments.
    """
    path = (globals(), serve.__globals__, bounded.__globals__)
    kept: TracebackType | None = None
    outside: list[TracebackType] = []
    while trace is not None:
        if all(trace.tb_frame.f_globals is not owned for owned in path):
            outside.append(trace)
        trace = trace.tb_next
    for entry in reversed(outside):
        kept = TracebackType(kept, entry.tb_frame, entry.tb_lasti, entry.tb_lineno)
    return kept


def _unavailable(detail: GatewayDetail) -> TurnStopped:
    return _GatewayStop(TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, detail=detail)


async def _raise_unless_ok(response: httpx2.Response) -> None:
    """A 200 passes; any other status ends the turn or the list as the status table says."""
    status = response.status_code
    if status == HTTPStatus.OK:
        return
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        wait = _wait_seconds(response.headers.get("Retry-After"))
        raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.BUSY, retry_after_s=wait)
    if status == HTTPStatus.UNPROCESSABLE_CONTENT and _is_spent_budget(await _error_of(response)):
        raise _GatewayStop(TurnStopKind.REFUSED, RejectionCode.BUDGET_SPENT)
    is_refusal = HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
    detail = GatewayDetail.GATEWAY_REFUSED if is_refusal else GatewayDetail.GATEWAY_FAILED
    raise _unavailable(detail)


def _is_spent_budget(error: JsonValue) -> bool:
    checked = checked_error_of(error)
    return checked is not None and checked.error_type == _BUDGET_EXCEEDED


async def _error_of(response: httpx2.Response) -> JsonValue:
    """The reply's ``error`` member, or none when the body is unreadable."""
    try:
        document = loaded_json(await _read_body(response, MAX_EVENT_BYTES))
        return document["error"] if isinstance(document, dict) else None
    except (TurnStopped, *_UNREADABLE_REPLY):
        return None


async def _read_body(response: httpx2.Response, limit: int) -> bytes:
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > limit:
                raise _malformed_reply()
    except httpx2.DecodingError:
        raise _malformed_reply() from None
    return bytes(body)


@contextlib.asynccontextmanager
async def _request_line(event: str) -> AsyncGenerator[StepTimer, None]:
    """One request's ``.ended`` line, written when the block ends and then any stop is raised.

    A stop inside the block is the line's outcome and reason; the host's close or a cancellation
    makes it ``cancelled``. The turn reports a stop itself, so a failed line is a warning.
    """
    stopped: TurnStopped | None = None
    async with StepTimer(event, _LOGGER) as step:
        step.handled()
        try:
            yield step
        except TurnStopped as caught:
            _end_line_as(step, caught)
            stopped = caught
    if stopped is not None:
        raise stopped


def _end_line_as(step: StepTimer, stopped: TurnStopped) -> None:
    if stopped.kind is TurnStopKind.REFUSED:
        step.refuse(stopped.code.value)
    else:
        step.fail(stopped.detail or stopped.code.value)


def _add_usage(step: StepTimer, usage: TurnUsage | None) -> None:
    if usage is not None:
        step.add(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_write_1h_tokens=usage.cache_write_1h_tokens,
        )


async def _streamed_round(
    open_request: Callable[[StepTimer], contextlib.AbstractAsyncContextManager[httpx2.Response]],
    offered: frozenset[str],
    earlier_call_ids: set[str],
    deadline: float,
    one_call_per_reply: bool,
) -> AsyncGenerator[str | _Round, None]:
    """Each text piece of a round as it streams, then the round, judged whole.

    Opening the request and each read run under the deadline, and no timeout spans a yield. A
    deadline reached while the app holds a piece fails the round's line when the app pulls on. The
    round's one line is written when the request ends, and a failure, the round's judgement
    included, is raised after its handler ends.
    """
    failure: TurnStopped | None = None
    completed: list[StreamedRound] = []
    async with _request_line(_MODEL_REQUEST_EVENT) as step, contextlib.AsyncExitStack() as stack:
        response = await _before(deadline, stack.enter_async_context(open_request(step)))
        chunks = await stack.enter_async_context(contextlib.aclosing(_chunks(response, deadline)))
        reading = read_round(chunks, round_limit_bytes=MAX_REQUEST_BYTES)
        try:
            async with contextlib.aclosing(reading) as parts:
                while not isinstance(part := await anext(parts), StreamedRound):
                    step.first_output()
                    yield part
                    _refuse_after(deadline)
                completed.append(part)
        except httpx2.DecodingError, MalformedRound:
            failure = _malformed_reply()
        except RoundErrorEvent:
            failure = _unavailable(GatewayDetail.GATEWAY_FAILED)
        if failure is not None:
            raise failure
        _add_usage(step, completed[0].usage)
        await _read_to_the_end(chunks)
        judged = _judged(completed[0], offered, earlier_call_ids, one_call_per_reply)
        if judged.calls_not_run is not None:
            step.add(calls_not_run=judged.calls_not_run)
    yield judged


async def _chunks(response: httpx2.Response, deadline: float) -> AsyncGenerator[bytes, None]:
    """The response's byte chunks, each read under the turn's deadline."""
    async with contextlib.aclosing(response.aiter_bytes()) as chunks:
        while (chunk := await _before(deadline, anext(chunks, None))) is not None:
            yield chunk


async def _read_to_the_end(chunks: AsyncIterator[bytes]) -> None:
    """Take what follows ``[DONE]`` so the pool keeps the connection; a bad tail only closes it."""
    remaining = _TRAILING_READ_BYTES
    with contextlib.suppress(TimeoutError, httpx2.HTTPError, OSError):
        async with asyncio.timeout(_TRAILING_READ_SECONDS):
            async for chunk in chunks:
                remaining -= len(chunk)
                if remaining < 0:
                    return


def _judged(
    streamed: StreamedRound,
    offered: frozenset[str],
    earlier_call_ids: set[str],
    one_call_per_reply: bool,
) -> _Round:
    try:
        if streamed.finish_reason == "tool_calls":
            return _calls_of(
                streamed.message, offered, earlier_call_ids, streamed.usage, one_call_per_reply
            )
        if streamed.finish_reason == "stop":
            return _answer_of(streamed.message["content"], streamed.usage)
    except _UNREADABLE_REPLY:
        pass
    raise _malformed_reply()


async def _picture_of(response: httpx2.Response) -> RunFile:
    """Exactly one entry: a canonical, non-empty ``b64_json`` picture within its bound; no URL."""
    body = await _read_body(response, _MAX_PICTURE_BODY_BYTES)
    try:
        document = loaded_json(body)
        entries = document["data"] if isinstance(document, dict) else None
        if not isinstance(entries, list) or len(entries) != 1:
            raise _malformed_reply()
        entry = entries[0]
        if not isinstance(entry, dict) or entry.get("url") is not None:
            raise _malformed_reply()
        file = RunFile.model_validate(
            {"name": _PICTURE_FILE_NAME, "content": entry["b64_json"]}, strict=True
        )
    except _UNREADABLE_REPLY:
        raise _malformed_reply() from None
    if not file.content or len(file.content) > MAX_RUN_FILES_BYTES:
        raise _malformed_reply()
    return file


def _answer_of(text: str | None, usage: TurnUsage | None) -> _Round:
    if text is None or not text.strip():
        raise _malformed_reply()
    return _Round(text=text, calls=(), usage=usage)


def _calls_of(
    message: AssembledMessage,
    offered: frozenset[str],
    earlier_call_ids: set[str],
    usage: TurnUsage | None,
    one_call_per_reply: bool,
) -> _Round:
    """Every call of a round is judged before any is shown or run, then split into the served."""
    raw_calls = message["tool_calls"]
    if not raw_calls:
        raise _malformed_reply()
    calls: list[RequestedCall] = []
    seen_ids = set(earlier_call_ids)
    for raw in raw_calls:
        function = raw["function"]
        name, call_id = function["name"], _CALL_ID.validate_python(raw["id"])
        if name not in offered or call_id in seen_ids:
            raise _malformed_reply()
        seen_ids.add(call_id)
        arguments = _ARGUMENTS.validate_json(function["arguments"])
        calls.append(RequestedCall(id=call_id, name=name, arguments=arguments))
    served, calls_not_run = (calls[:1], len(calls) - 1) if one_call_per_reply else (calls, None)
    return _Round(
        text=_ROUND_TEXT.validate_python(message["content"]) or "",
        calls=tuple(calls),
        served=tuple(served),
        calls_not_run=calls_not_run,
        usage=usage,
    )


def _malformed_reply() -> TurnStopped:
    return _GatewayStop(
        TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR, detail=GatewayDetail.MALFORMED_REPLY
    )


def _wait_seconds(retry_after: str | None) -> int | None:
    """Delay-seconds only: an HTTP-date, a sign or a missing header gives none."""
    if retry_after is None or not retry_after.isascii() or not retry_after.isdigit():
        return None
    digits = retry_after.lstrip("0") or "0"
    if len(digits) > len(str(MAX_RETRY_AFTER_S)):
        return MAX_RETRY_AFTER_S
    return min(int(digits), MAX_RETRY_AFTER_S)
