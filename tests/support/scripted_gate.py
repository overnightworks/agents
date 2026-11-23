"""A gate before an app's member front on a loopback socket: bearer to person, scripted answers.

The front, its resources and its tools are the library's; the gate lets a test script the answer
to a request method so that the app misbehaves once, and records what each request carried.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
from mcp_types import (
    ErrorData,
    JSONRPCError,
    NotificationParams,
    ResourceUpdatedNotification,
    ResourceUpdatedNotificationParams,
    SubscriptionFilter,
    SubscriptionsAcknowledgedNotification,
    SubscriptionsAcknowledgedNotificationParams,
    SubscriptionsListenResult,
    ToolListChangedNotification,
)
from pydantic import BaseModel

from agents.mcp_server import McpPersonFront, OfferedTools, PersonTool, ResourceChanges
from agents.mcp_wire import MCP_ENDPOINT_PATH
from agents.tools import Principal
from tests.support.mcp_server_support import (
    ISSUER,
    MemberApp,
    loopback_origin,
    loopback_server,
)

DISCOVER = "server/discover"
READ = "resources/read"
CALL = "tools/call"
LIST_TOOLS = "tools/list"
LISTEN = "subscriptions/listen"
NO_METHOD = ""
"""The key a request that carries no JSON-RPC message, a stream reconnect, is scripted under."""
_SUBJECT_KEY = "test.subject"
_SHUTDOWN_SECONDS = 0.1
_JSON = "application/json"
_EVENT_STREAM = "text/event-stream"


class ConnectionCut(Exception):  # noqa: N818 - raised in the app to make the server drop the connection
    """Ends the answer without its end, so the client sees a lost connection."""


@dataclass(frozen=True)
class Rpc:
    """The JSON-RPC request a handler answers."""

    id: int | str
    method: str
    params: dict[str, Any]


@dataclass
class GateReply:
    """The answer to one request, written through the ASGI ``send``."""

    rpc: Rpc
    send: Callable[[dict[str, Any]], Awaitable[None]]

    async def result(self, result: BaseModel) -> None:
        await self.raw_result(result.model_dump(by_alias=True, mode="json", exclude_none=True))

    async def raw_result(
        self, result: dict[str, Any], *, answering: int | str | None = None
    ) -> None:
        """Answers the request, or the request ``answering`` names, with the result."""
        request_id = self.rpc.id if answering is None else answering
        await self._whole(HTTPStatus.OK, {"jsonrpc": "2.0", "id": request_id, "result": result})

    async def error(
        self, code: int, *, status: HTTPStatus = HTTPStatus.BAD_REQUEST, data: object = None
    ) -> None:
        error = JSONRPCError(
            jsonrpc="2.0", id=self.rpc.id, error=ErrorData(code=code, message="refused", data=data)
        )
        await self._whole(status, error.model_dump(by_alias=True, mode="json", exclude_none=True))

    async def status(self, status: HTTPStatus, headers: dict[str, str] | None = None) -> None:
        await self._start(status, None, headers)
        await self.send({"type": "http.response.body", "body": b""})

    async def body(self, status: HTTPStatus, content_type: str | None, content: bytes) -> None:
        """Answers with the content as it is, of the content type given or of none."""
        await self._start(status, content_type)
        await self.send({"type": "http.response.body", "body": content})

    async def undecodable(self, status: HTTPStatus) -> None:
        """Answers with a JSON body whose declared gzip encoding does not decode."""
        headers = [(b"content-type", _JSON.encode()), (b"content-encoding", b"gzip")]
        await self.send({"type": "http.response.start", "status": status.value, "headers": headers})
        await self.send({"type": "http.response.body", "body": b"not gzip at all"})

    async def open_stream(self) -> None:
        await self._start(HTTPStatus.OK, _EVENT_STREAM)

    async def end(self) -> None:
        await self.send({"type": "http.response.body", "body": b"", "more_body": False})

    async def event(self, document: dict[str, Any], event_id: str | None = None) -> None:
        identified = f"id: {event_id}\nretry: 1\n" if event_id else ""
        body = f"{identified}event: message\ndata: {json.dumps(document)}\n\n".encode()
        await self.send({"type": "http.response.body", "body": body, "more_body": True})

    async def acknowledge(self, honored: list[str], *, tools: bool = False) -> None:
        """Acknowledges the listen stream with these resources, and the tools notice if asked."""
        notification = SubscriptionsAcknowledgedNotification(
            params=SubscriptionsAcknowledgedNotificationParams(
                notifications=SubscriptionFilter(
                    resource_subscriptions=honored or None, tools_list_changed=tools or None
                ),
                _meta=self._stream_meta,
            )
        )
        await self._notify(notification)

    async def pulse(self, uri: str) -> None:
        """Tells the listener that the resource changed."""
        notification = ResourceUpdatedNotification(
            params=ResourceUpdatedNotificationParams(uri=uri, _meta=self._stream_meta)
        )
        await self._notify(notification)

    async def tools_notice(self) -> None:
        """Tells the listener that the app's tools changed."""
        await self._notify(
            ToolListChangedNotification(params=NotificationParams(_meta=self._stream_meta))
        )

    async def complete(self) -> None:
        """Ends the listen stream the way the app ends it, with its result."""
        result = SubscriptionsListenResult(_meta=self._stream_meta)
        await self.event(
            {
                "jsonrpc": "2.0",
                "id": self.rpc.id,
                "result": result.model_dump(by_alias=True, mode="json", exclude_none=True),
            }
        )
        await self.end()

    async def cut(self) -> None:
        """Starts a stream and drops the connection; a cut before the answer starts is a 500."""
        await self.open_stream()
        raise ConnectionCut

    @property
    def _stream_meta(self) -> dict[str, Any]:
        return {SUBSCRIPTION_ID_META_KEY: self.rpc.id}

    async def _notify(self, notification: BaseModel) -> None:
        await self.event(
            notification.model_dump(by_alias=True, mode="json", exclude_none=True)
            | {"jsonrpc": "2.0"}
        )

    async def _whole(self, status: HTTPStatus, document: dict[str, Any]) -> None:
        await self._start(status, _JSON)
        await self.send({"type": "http.response.body", "body": json.dumps(document).encode()})

    async def _start(
        self, status: HTTPStatus, content_type: str | None, extra: dict[str, str] | None = None
    ) -> None:
        headers = [(b"content-type", content_type.encode())] if content_type else []
        headers += [(name.encode(), value.encode()) for name, value in (extra or {}).items()]
        await self.send({"type": "http.response.start", "status": status.value, "headers": headers})


type Handler = Callable[[GateReply], Awaitable[None]]


async def hangs(_reply: GateReply) -> None:
    await asyncio.Event().wait()


def hangs_once_entered(entered: asyncio.Event) -> Handler:
    """An answer that tells a test its request arrived and then never ends."""

    async def hang(reply: GateReply) -> None:
        entered.set()
        await hangs(reply)

    return hang


async def cuts(reply: GateReply) -> None:
    await reply.cut()


@dataclass(frozen=True)
class Seen:
    """What one request carried to the app."""

    method: str
    bearer: str
    traceparent: str | None
    cookie: str | None


@dataclass
class ScriptedGate:
    """What a test scripts and reads of an app's front: who is let in, what it answers, what it saw.

    A request that no script answers goes through to the real front as the person its bearer was
    let in as; ``url`` is where the app is served.
    """

    app: MemberApp
    bearers: dict[str, str] = field(default_factory=dict[str, str])
    presented: set[str] = field(default_factory=set[str])
    """Every bearer a request carried, known or not."""
    seen: list[Seen] = field(default_factory=list[Seen])
    scripted: defaultdict[str, list[Handler]] = field(
        default_factory=lambda: defaultdict(list[Handler])
    )
    sets_cookie: str | None = None
    """A cookie every answer, scripted or the front's, sets, as an app behind a router does."""
    url: str = ""
    tools: Sequence[PersonTool] = ()
    """The tools the real front serves."""
    changes: ResourceChanges | None = None
    """What the real front listens through; none leaves it without ``subscriptions/listen``."""
    offered_tools: OfferedTools | None = None

    def __post_init__(self) -> None:
        self.front = McpPersonFront(
            self.tools,
            resolve_person=lambda scope: Principal(iss=ISSUER, sub=scope[_SUBJECT_KEY]),
            resources=self.app,
            changes=self.changes,
            offered_tools=self.offered_tools,
        )

    def let_in(self, bearer: str, subject: str) -> None:
        self.bearers[bearer] = subject

    def script(self, method: str, *handlers: Handler) -> None:
        """Each of the next requests of this method is answered by one handler, in order."""
        self.scripted[method].extend(handlers)

    def count(self, method: str) -> int:
        return sum(each.method == method for each in self.seen)

    async def __call__(
        self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        body = await _body_of(receive)
        document = json.loads(body) if body else {}
        method = document.get("method", NO_METHOD)
        if self.sets_cookie is not None:
            send = _setting_cookie(send, self.sets_cookie)
        headers = {name.decode(): value.decode() for name, value in scope["headers"]}
        bearer = headers.get("authorization", "").removeprefix("Bearer ")
        self.presented.add(bearer)
        self.seen.append(Seen(method, bearer, headers.get("traceparent"), headers.get("cookie")))
        reply = GateReply(Rpc(document.get("id", 0), method, document.get("params") or {}), send)
        if bearer not in self.bearers:
            await reply.status(HTTPStatus.UNAUTHORIZED)
        elif self.scripted[method]:
            await self.scripted[method].pop(0)(reply)
        else:
            scope[_SUBJECT_KEY] = self.bearers[bearer]
            await self.front(scope, _replayed(body, receive), send)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator[str]:
        """The app's MCP URL, served until the end."""
        server = loopback_server(self.front, self)
        # A scripted answer may never end, and a stop must not wait for it.
        server.config.timeout_graceful_shutdown = _SHUTDOWN_SECONDS
        async with loopback_origin(server) as origin:
            self.url = f"{origin}{MCP_ENDPOINT_PATH}"
            yield self.url


async def _body_of(receive: Callable[..., Any]) -> bytes:
    body = b""
    more = True
    while more:
        message = await receive()
        body += message.get("body", b"")
        more = message.get("more_body", False)
    return body


def _setting_cookie(send: Callable[..., Any], cookie: str) -> Callable[..., Any]:
    async def setting(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            message = {
                **message,
                "headers": [*message["headers"], (b"set-cookie", cookie.encode())],
            }
        await send(message)

    return setting


def _replayed(body: bytes, receive: Callable[..., Any]) -> Callable[..., Any]:
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def replay() -> dict[str, Any]:
        return pending.pop() if pending else await receive()

    return replay
