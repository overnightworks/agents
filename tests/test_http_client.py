"""The HTTP policy every client of ``agents`` shares: no proxy variable, no redirect, no cookie."""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from agents.http_client import policy_client

ORIGIN = "https://app.test"


def answering(handler) -> httpx2.MockTransport:
    return httpx2.MockTransport(handler)


async def get_twice(client: httpx2.AsyncClient) -> list[httpx2.Response]:
    async with client:
        return [await client.get("/first"), await client.get("/second")]


def test_a_proxy_variable_of_the_environment_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")

    async def reach_the_peer_itself() -> int:
        async def answer(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server, policy_client(timeout=5) as client:
            return (await client.get(f"http://127.0.0.1:{port}/")).status_code

    assert asyncio.run(reach_the_peer_itself()) == 204


def test_a_redirect_is_the_answer_and_is_not_followed() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(307, headers={"Location": "https://elsewhere.test/moved"})

    answers = asyncio.run(
        get_twice(policy_client(base_url=ORIGIN, transport=answering(handler), timeout=5))
    )

    assert [answer.status_code for answer in answers] == [307, 307]
    assert [request.url.host for request in seen] == ["app.test", "app.test"]


def test_a_cookie_the_peer_sets_is_never_sent_back() -> None:
    cookies: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        cookies.append(request.headers.get("cookie"))
        return httpx2.Response(200, headers={"Set-Cookie": "route=pod-1; Path=/"})

    asyncio.run(get_twice(policy_client(base_url=ORIGIN, transport=answering(handler), timeout=5)))

    assert cookies == [None, None]


def test_the_base_address_the_timeout_and_the_auth_stay_the_caller_s() -> None:
    authorizations: list[str | None] = []

    class Bearer(httpx2.Auth):
        def auth_flow(self, request: httpx2.Request):
            request.headers["Authorization"] = "Bearer chosen"
            yield request

    def handler(request: httpx2.Request) -> httpx2.Response:
        authorizations.append(request.headers.get("authorization"))
        return httpx2.Response(200)

    client = policy_client(base_url=ORIGIN, transport=answering(handler), timeout=7, auth=Bearer())
    asyncio.run(get_twice(client))

    assert authorizations == ["Bearer chosen", "Bearer chosen"]
    assert client.timeout == httpx2.Timeout(7)
    assert str(client.base_url).startswith(ORIGIN)
