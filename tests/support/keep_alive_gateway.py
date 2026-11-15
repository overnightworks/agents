"""A loopback gateway that keeps its connections open and counts them."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from tests.support.gateway_turn_support import a_chunk, an_event_stream

_CONTENT_LENGTH = re.compile(rb"content-length:\s*(\d+)", re.IGNORECASE)


@dataclass
class KeepAliveGateway:
    """Answers every request with one short reply on the connection it came in on.

    ``accepted`` counts connections, ``ended`` the ones the peer or this gateway has closed;
    ``set_cookie`` is sent with every reply and ``cookies`` records what each request carried.
    A ``tail`` makes the reply end only when the connection does: after ``[DONE]`` it sends
    ``tail_bytes`` more bytes at once, then trickles a byte at a time for as long as the
    connection lasts.
    """

    set_cookie: str | None = None
    tail: bool = False
    tail_bytes: int = 0
    port: int = 0
    accepted: int = 0
    ended: int = 0
    cookies: list[bytes | None] = field(default_factory=list)
    _ending: asyncio.Condition = field(default_factory=asyncio.Condition)
    _open: list[asyncio.StreamWriter] = field(default_factory=list)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def close_idle(self) -> None:
        """Close every open connection from this side and return once each has ended."""
        before = self.ended
        waiting_for = len(self._open)
        for writer in tuple(self._open):
            writer.close()
        async with self._ending:
            await self._ending.wait_for(lambda: self.ended - before >= waiting_for)

    async def wait_until_ended(self, count: int) -> None:
        """Return once ``count`` connections have ended."""
        async with self._ending:
            await self._ending.wait_for(lambda: self.ended >= count)

    async def _reply_with_a_tail(
        self, writer: asyncio.StreamWriter, body: bytes, extra: str
    ) -> None:
        head = f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n{extra}\r\n"
        writer.write(head.encode() + body)
        writer.write(b"\n" * self.tail_bytes)
        while True:
            writer.write(b"\n")
            await writer.drain()
            await asyncio.sleep(0.05)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        self._open.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = _CONTENT_LENGTH.search(head)
                if length:
                    await reader.readexactly(int(length.group(1)))
                cookie = re.search(rb"(?im)^cookie:\s*(.*)\r\n", head)
                self.cookies.append(cookie.group(1) if cookie else None)
                body = an_event_stream(a_chunk("hi"), a_chunk(finish_reason="stop"))
                extra = f"Set-Cookie: {self.set_cookie}\r\n" if self.set_cookie else ""
                if self.tail:
                    await self._reply_with_a_tail(writer, body, extra)
                    return
                writer.write(
                    (
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        f"Content-Length: {len(body)}\r\n{extra}\r\n"
                    ).encode()
                    + body
                )
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()
            self._open.remove(writer)
            async with self._ending:
                self.ended += 1
                self._ending.notify_all()


@contextlib.asynccontextmanager
async def serving_keep_alive(
    set_cookie: str | None = None, *, tail: bool = False, tail_bytes: int = 0
) -> AsyncIterator[KeepAliveGateway]:
    gateway = KeepAliveGateway(set_cookie=set_cookie, tail=tail, tail_bytes=tail_bytes)
    server = await asyncio.start_server(gateway.serve, "127.0.0.1", 0)
    gateway.port = server.sockets[0].getsockname()[1]
    async with server:
        try:
            yield gateway
        finally:
            server.close_clients()
