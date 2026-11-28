"""A member session whose tools list was still on its way when the app announced a change to its
tools: an argument the app does not declare is left out, and the omission is told."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from agents.member import (
    ACCEPT_INVITATION_TOOL,
    SPEAK_TOOL,
    PlaceRef,
)
from agents.member_outcomes import ArgumentLeftOut
from tests.support import member_session_support
from tests.support.mcp_server_support import MemberApp
from tests.support.member_session_support import (
    LIMIT_SECONDS,
    MemberTools,
    a_figure,
    accepting,
    opened,
    served,
)
from tests.support.scripted_gate import (
    LIST_TOOLS,
    LISTEN,
    GateReply,
    ScriptedGate,
    acknowledging,
    listing,
)

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
bearer = member_session_support.bearer
place = member_session_support.place


@pytest.fixture
def tools(request: pytest.FixtureRequest) -> MemberTools:
    return MemberTools(**getattr(request, "param", {}))


@pytest.fixture
async def gate(app: MemberApp, tools: MemberTools, caplog: pytest.LogCaptureFixture) -> Any:
    async with served(app, caplog, tools=tools.served()) as gate:
        yield gate


@pytest.mark.parametrize(
    ("tools", "tool", "argument", "calls"),
    [
        pytest.param(
            {"undeclared": frozenset({"figure"})},
            ACCEPT_INVITATION_TOOL,
            "figure",
            lambda session, place: session.accept(accepting(place, figure=a_figure())),
            id="an-accept-with-a-figure",
        ),
        pytest.param(
            {"undeclared": frozenset({"keeps_queue"})},
            SPEAK_TOOL,
            "keeps_queue",
            lambda session, place: session.speak(
                place, "hello", interrupted="hold-1", keeps_queue=True
            ),
            id="a-speak-that-keeps-the-queue",
        ),
    ],
    indirect=["tools"],
)
async def test_an_undeclared_argument_is_left_out_though_a_tools_notice_came_while_listing(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    tool: str,
    argument: str,
    calls: Any,
) -> None:
    listing_started, noticed, notice_taken = asyncio.Event(), asyncio.Event(), asyncio.Event()
    left_out: list[ArgumentLeftOut] = []

    async def notifies_while_listing(reply: GateReply) -> None:
        async with asyncio.timeout(LIMIT_SECONDS):
            await listing_started.wait()
        await reply.tools_notice()
        noticed.set()

    async def lists_after_the_notice(reply: GateReply) -> None:
        listing_started.set()
        async with asyncio.timeout(LIMIT_SECONDS):
            await notice_taken.wait()
        await listing(*tools.served())(reply)

    gate.script(LISTEN, acknowledging(tools=True, then=notifies_while_listing))
    gate.script(LIST_TOOLS, lists_after_the_notice)

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        async with session.listen() as stream:
            call = asyncio.create_task(calls(session, place))
            async with asyncio.timeout(LIMIT_SECONDS):
                await noticed.wait()
                await anext(stream)
            notice_taken.set()
            await call

    assert [argument in sent for sent in tools.called(tool)] == [False]
    assert ArgumentLeftOut(tool, argument) in left_out
