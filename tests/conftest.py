"""The fixtures of the member session's suites: an app served behind a scripted gate and a bearer it
lets in."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from agents.member import PlaceRef
from tests.support.mcp_server_support import MemberApp
from tests.support.member_session_support import ROOM, a_bearer_let_in, served
from tests.support.scripted_gate import ScriptedGate


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def place() -> PlaceRef:
    return PlaceRef(kind="call", id=ROOM)


@pytest.fixture
def table() -> PlaceRef:
    return PlaceRef(kind="call", id="table-1")


@pytest.fixture
def app() -> MemberApp:
    return MemberApp()


@pytest.fixture
async def gate(app: MemberApp, caplog: pytest.LogCaptureFixture) -> AsyncIterator[ScriptedGate]:
    async with served(app, caplog) as gate:
        yield gate


@pytest.fixture
def bearer(gate: ScriptedGate) -> SecretStr:
    return a_bearer_let_in(gate)
