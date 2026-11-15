"""One pod of a minimal test host: the person front on a real Redis, served by its own uvicorn.

Run as ``python -m tests.integration.real_redis.pod <port> <redis url> <key prefix>``. The pod
serves the standing of one member; ``POST /write`` counts a write in Redis and announces it
through ``ResourceChanges``, the way a host's own handler does.
"""

from __future__ import annotations

import sys
from http import HTTPStatus

import uvicorn
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.mcp_server import McpPersonFront, ResourceChanges
from agents.mcp_server.redis_place_store import RedisPlaceStore
from agents.mcp_wire import MCP_ENDPOINT_PATH
from agents.member import STANDING_URI
from tests.support.mcp_server_support import MemberApp, a_member, readable_uris, resolve_member

WRITE_PATH = "/write"
WRITES_KEY = "writes"
WRITTEN_MEMBER = a_member("alice")


def a_pod(redis_url: str, key_prefix: str) -> Starlette:
    redis = Redis.from_url(
        redis_url,
        retry=Retry(ExponentialBackoff(), 3),
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    changes = ResourceChanges(RedisPlaceStore(redis, key_prefix=key_prefix))
    resources = MemberApp(readable={WRITTEN_MEMBER: readable_uris(STANDING_URI)})
    front = McpPersonFront([], resolve_person=resolve_member, resources=resources, changes=changes)

    async def write(_request: Request) -> JSONResponse:
        writes = await redis.incr(WRITES_KEY)
        await changes.changed(WRITTEN_MEMBER, STANDING_URI)
        return JSONResponse({"writes": writes}, status_code=HTTPStatus.OK)

    return Starlette(
        routes=[Route(MCP_ENDPOINT_PATH, front), Route(WRITE_PATH, write, methods=["POST"])],
        lifespan=lambda _app: front.running(),
    )


if __name__ == "__main__":
    port, redis_url, key_prefix = sys.argv[1:]
    uvicorn.run(a_pod(redis_url, key_prefix), host="127.0.0.1", port=int(port), log_level="warning")
