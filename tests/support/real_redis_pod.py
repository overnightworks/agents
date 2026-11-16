"""One pod of a minimal test host: the person front on a real Redis, served by its own uvicorn.

Run as ``python -m tests.support.real_redis_pod <port> <redis url> <key prefix>``. The pod
serves the standing of one member; ``POST /write`` counts a write in Redis and announces it
through ``ResourceChanges``, the way a host's own handler does. ``GET /listeners`` answers who
listens to the standing and ``GET /listening-changes`` every call of ``listening_changed`` the pod
received, in order, and ``GET /listening-traces`` the trace id each of those calls ran in; the pod
runs each request in its trace as a host does, with logline's ``request_trace``. ``POST /renew``
renews the written member's lease and answers its renewal, ``GET /lease`` its standing,
``GET /leases`` the members whose lease runs and ``GET /lapses`` every call of ``lease_ended`` the
pod received, in order.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from http import HTTPStatus

import uvicorn
from logline.trace import current_trace_id, request_trace
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from agents.mcp_server import McpPersonFront, MemberLeases, ResourceChanges
from agents.mcp_server.redis_place_store import RedisPlaceStore
from agents.mcp_wire import MCP_ENDPOINT_PATH
from agents.member import STANDING_URI
from agents.turn import Principal
from tests.support.mcp_server_support import MemberApp, a_member, readable_uris, resolve_member

WRITE_PATH = "/write"
LISTENERS_PATH = "/listeners"
LISTENING_CHANGES_PATH = "/listening-changes"
LISTENING_TRACES_PATH = "/listening-traces"
RENEW_PATH = "/renew"
LEASE_PATH = "/lease"
LEASES_PATH = "/leases"
LAPSES_PATH = "/lapses"
WRITES_KEY = "writes"
WRITTEN_MEMBER = a_member("alice")


class _RequestTrace:
    """The host's ASGI layer that runs each request in its own or the incoming trace."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        with request_trace(scope["headers"]):
            await self._app(scope, receive, send)


def _lease_routes(store: RedisPlaceStore) -> tuple[MemberLeases, list[Route]]:
    """The host's leases on the store, and the routes that renew, read and list them and report the
    lapses its ``lease_ended`` received."""
    lapses: list[tuple[str, str, str]] = []

    async def lease_ended(principal: Principal, generation: str) -> None:
        lapses.append((principal.iss, principal.sub, generation))

    leases = MemberLeases(store, lease_ended=lease_ended)

    async def renew(_request: Request) -> JSONResponse:
        renewal = await leases.renew(WRITTEN_MEMBER)
        return JSONResponse({"started": renewal.started, "generation": renewal.generation})

    async def lease(_request: Request) -> JSONResponse:
        standing = await leases.state(WRITTEN_MEMBER)
        return JSONResponse({"holds": standing.holds, "generation": standing.generation})

    async def live_leases(_request: Request) -> JSONResponse:
        roster = await leases.live()
        return JSONResponse(sorted([principal.iss, principal.sub] for principal in roster.holders))

    async def lapses_received(_request: Request) -> JSONResponse:
        return JSONResponse(lapses)

    routes = [
        Route(RENEW_PATH, renew, methods=["POST"]),
        Route(LEASE_PATH, lease),
        Route(LEASES_PATH, live_leases),
        Route(LAPSES_PATH, lapses_received),
    ]
    return leases, routes


def a_pod(redis_url: str, key_prefix: str) -> Starlette:
    redis = Redis.from_url(
        redis_url,
        retry=Retry(ExponentialBackoff(), 3),
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    listening_changes: list[tuple[str, str, str, bool]] = []
    listening_traces: list[str | None] = []

    def record(principal: Principal, uri: str, *, listens: bool) -> None:
        listening_changes.append((principal.iss, principal.sub, uri, listens))
        listening_traces.append(current_trace_id())

    store = RedisPlaceStore(redis, key_prefix=key_prefix)
    changes = ResourceChanges(
        store,
        listening_changed=lambda principal, uri, listens: record(principal, uri, listens=listens),
    )
    resources = MemberApp(readable={WRITTEN_MEMBER: readable_uris(STANDING_URI)})
    front = McpPersonFront([], resolve_person=resolve_member, resources=resources, changes=changes)
    leases, lease_routes = _lease_routes(store)

    @asynccontextmanager
    async def serving(_app: Starlette) -> AsyncGenerator[None, None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(front.running())
            await stack.enter_async_context(leases.running())
            yield

    async def write(_request: Request) -> JSONResponse:
        writes = await redis.incr(WRITES_KEY)
        await changes.changed(WRITTEN_MEMBER, STANDING_URI)
        return JSONResponse({"writes": writes}, status_code=HTTPStatus.OK)

    async def listeners(_request: Request) -> JSONResponse:
        who = await changes.listeners(STANDING_URI)
        return JSONResponse(sorted([principal.iss, principal.sub] for principal in who))

    async def changes_received(_request: Request) -> JSONResponse:
        return JSONResponse(listening_changes)

    async def traces_received(_request: Request) -> JSONResponse:
        return JSONResponse(listening_traces)

    return Starlette(
        routes=[
            Route(MCP_ENDPOINT_PATH, front),
            Route(WRITE_PATH, write, methods=["POST"]),
            Route(LISTENERS_PATH, listeners),
            Route(LISTENING_CHANGES_PATH, changes_received),
            Route(LISTENING_TRACES_PATH, traces_received),
            *lease_routes,
        ],
        lifespan=serving,
        middleware=[Middleware(_RequestTrace)],
    )


if __name__ == "__main__":
    port, redis_url, key_prefix = sys.argv[1:]
    uvicorn.run(a_pod(redis_url, key_prefix), host="127.0.0.1", port=int(port), log_level="warning")
