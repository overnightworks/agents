"""Stand-ins for the SDK's transport that the member session opens: late answers, chosen leaves, a
waiting close and a child that fails as it closes.

Each stands in for ``streamable_http_client`` as ``agents.mcp_client.session`` imports it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import anyio

from agents.mcp_client import session


def transport_closing_with_late_answers(*, echo: type[Exception], echoes: int = 1) -> Any:
    """A stand-in whose close raises ``echoes`` late-answer errors.

    The echoes leave beside the error that ended the session, or alone when the session was
    cancelled.
    """
    real = session.streamable_http_client

    @asynccontextmanager
    async def failing(url: str, *, http_client: Any):
        async with real(url, http_client=http_client) as streams:
            try:
                yield streams
            except Exception as error:
                raise ExceptionGroup(
                    "the transport", [error, *(echo() for _ in range(echoes))]
                ) from None
            except asyncio.CancelledError:
                raise ExceptionGroup("the transport", [echo() for _ in range(echoes)]) from None

    return failing


def transport_closing_with_leaves(*leaves: Exception) -> Any:
    """A stand-in whose close raises exactly these leaves when its body was cancelled."""
    real = session.streamable_http_client

    @asynccontextmanager
    async def failing(url: str, *, http_client: Any):
        async with real(url, http_client=http_client) as streams:
            try:
                yield streams
            except asyncio.CancelledError:
                raise ExceptionGroup("the transport", list(leaves)) from None

    return failing


def transport_whose_close_loses_a_child(error: type[Exception]) -> Any:
    """A stand-in whose close awaits inside an anyio group, as the SDK's close does, while a child
    of that group fails with ``error``."""
    real = session.streamable_http_client

    @asynccontextmanager
    async def losing(url: str, *, http_client: Any):
        async with (
            real(url, http_client=http_client) as streams,
            anyio.create_task_group() as group,
        ):
            try:
                yield streams
            finally:

                async def child() -> None:
                    await asyncio.sleep(0)
                    raise error()

                group.start_soon(child)
                await asyncio.Event().wait()

    return losing


class ClosingHold:
    """What a test sees of, and does to, one transport that waits while it closes."""

    def __init__(self) -> None:
        self.closing = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    def transport(self) -> Any:
        """A stand-in whose close waits for ``release``.

        ``closing`` is set when the close starts to wait, and ``cancelled`` when the task was
        cancelled while it waited.
        """
        real = session.streamable_http_client

        @asynccontextmanager
        async def waiting(url: str, *, http_client: Any):
            async with real(url, http_client=http_client) as streams:
                try:
                    yield streams
                finally:
                    await self._held()

        return waiting

    async def _held(self) -> None:
        self.closing.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
