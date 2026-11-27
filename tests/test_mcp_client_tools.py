"""The member session's tools: what the app declares, and the outcome of a tool it invokes, a
picture and a refusal included."""

from __future__ import annotations

import base64
import secrets
from http import HTTPStatus
from typing import Any

import pytest
from mcp_types import ListToolsResult, Tool, ToolAnnotations
from pydantic import SecretStr

from agents.mcp_client import MemberSession
from agents.mcp_server import ResourceChanges
from agents.member import MEMBER_TOOL_ARGUMENTS, SPEAK_TOOL
from agents.member_outcomes import INVALID_ANSWER_CODE, StatusFailure, UnusableAnswer
from agents.tools import ToolEffect, ToolOutcome
from agents.turn import TOOL_IMAGE_MEDIA_TYPE
from tests.support import member_session_support
from tests.support.mcp_server_support import MemberApp, ToolOffer
from tests.support.member_session_support import MemberTools, opened, served
from tests.support.member_wire import speak_wire
from tests.support.scripted_gate import CALL, LIST_TOOLS, GateReply, ScriptedGate
from tests.support.tool_images import a_tool_image

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
bearer = member_session_support.bearer
changes = member_session_support.changes


@pytest.fixture
def tools() -> MemberTools:
    return MemberTools()


@pytest.fixture
async def gate(app: MemberApp, tools: MemberTools, caplog: pytest.LogCaptureFixture) -> Any:
    async with served(app, caplog, tools=tools.served()) as gate:
        yield gate


def base64_of(picture: bytes) -> str:
    return base64.b64encode(picture).decode("ascii")


async def test_the_declared_tools_are_the_ones_the_app_lists(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async with opened(gate, bearer) as session:
        declared = await session.declared_tools()

    assert {each.name for each in declared} == set(MEMBER_TOOL_ARGUMENTS)


async def test_a_tool_marked_read_only_is_declared_read_only(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def lists_a_read_only_tool(reply: GateReply) -> None:
        tool = Tool(
            name="peek",
            description="Looks.",
            input_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=True),
        )
        await reply.result(ListToolsResult(tools=[tool]))

    gate.script(LIST_TOOLS, lists_a_read_only_tool)

    async with opened(gate, bearer) as session:
        declared = await session.declared_tools()

    assert [(each.name, each.effect) for each in declared] == [("peek", ToolEffect.READ_ONLY)]


async def test_a_declaration_that_fails_validation_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def lists_a_tool_without_a_description(reply: GateReply) -> None:
        tool = Tool(name="peek", input_schema={"type": "object"})
        await reply.result(ListToolsResult(tools=[tool]))

    gate.script(LIST_TOOLS, lists_a_tool_without_a_description)

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.declared_tools()

    assert failure.value.code == INVALID_ANSWER_CODE


async def test_a_listing_that_names_a_tool_twice_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def lists_one_tool_twice(reply: GateReply) -> None:
        tool = Tool(name="peek", description="peek", input_schema={"type": "object"})
        await reply.result(ListToolsResult(tools=[tool, tool]))

    gate.script(LIST_TOOLS, lists_one_tool_twice)

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.declared_tools()

    assert failure.value.code == INVALID_ANSWER_CODE


@pytest.mark.parametrize(
    "outcome",
    [ToolOutcome("said", is_error=False), ToolOutcome("gone", is_error=True)],
    ids=["an answer", "an error"],
)
async def test_an_invoked_tool_gives_the_outcome_the_app_answered_an_error_one_too(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, outcome: ToolOutcome
) -> None:
    tools.outcomes[SPEAK_TOOL] = [outcome]

    async with opened(gate, bearer) as session:
        invoked = await session.invoke(SPEAK_TOOL, speak_wire())

    assert invoked == outcome


@pytest.mark.parametrize("text", ["", "seen"], ids=["a picture alone", "a picture with text"])
@pytest.mark.parametrize("is_error", [False, True], ids=["answer", "error"])
async def test_an_invoked_tool_gives_the_picture_the_app_answered_with_its_text(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, text: str, is_error: bool
) -> None:
    picture = a_tool_image()
    tools.outcomes[SPEAK_TOOL] = [ToolOutcome(text, is_error=is_error, image=picture)]

    async with opened(gate, bearer) as session:
        invoked = await session.invoke(SPEAK_TOOL, speak_wire())

    assert (invoked.content, invoked.is_error) == (text, is_error)
    assert invoked.image is not None
    assert invoked.image.content == picture.content


async def test_the_picture_an_invoked_tool_gives_shows_no_byte_in_its_representation(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools
) -> None:
    picture = a_tool_image()
    tools.outcomes[SPEAK_TOOL] = [ToolOutcome("", is_error=False, image=picture)]

    async with opened(gate, bearer) as session:
        invoked = await session.invoke(SPEAK_TOOL, speak_wire())

    shown = repr(invoked)
    assert repr(picture.content) not in shown
    assert base64_of(picture.content) not in shown


def a_picture_block(**fields: str) -> dict[str, str]:
    """An image block of the picture, with these fields replaced."""
    picture = {
        "type": "image",
        "data": base64_of(a_tool_image().content),
        "mimeType": TOOL_IMAGE_MEDIA_TYPE,
    }
    return picture | fields


def a_picture_answer(**fields: str) -> dict[str, object]:
    text = {"type": "text", "text": "seen"}
    return {"resultType": "complete", "content": [text, a_picture_block(**fields)]}


def a_picture_of_two_blocks() -> dict[str, object]:
    block = a_picture_block()
    return {"resultType": "complete", "content": [block, block]}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            lambda: a_picture_answer(data=base64_of(a_tool_image(16).content).rstrip("=")),
            id="non-canonical base64",
        ),
        pytest.param(
            lambda: a_picture_answer(
                data=base64_of(b"\x00" + secrets.token_bytes(16)),
            ),
            id="no JPEG start marker",
        ),
        pytest.param(lambda: a_picture_answer(data=""), id="empty data"),
        pytest.param(lambda: a_picture_answer(mimeType="image/png"), id="another media type"),
        pytest.param(a_picture_of_two_blocks, id="two pictures"),
    ],
)
async def test_a_picture_that_breaks_the_wire_is_an_unusable_answer_and_no_validation_error(
    gate: ScriptedGate, bearer: SecretStr, answer: Any
) -> None:
    served_answer = answer()
    gate.script(CALL, lambda reply: reply.raw_result(served_answer))

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.invoke(SPEAK_TOOL, speak_wire())

    assert failure.value.code == INVALID_ANSWER_CODE
    assert failure.value.__cause__ is None


async def test_audio_and_resource_blocks_of_a_result_are_ignored_beside_its_text(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    blocks = [
        {"type": "text", "text": "seen"},
        {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
        {"type": "resource_link", "uri": "file:///a", "name": "a"},
    ]
    gate.script(CALL, lambda reply: reply.raw_result({"resultType": "complete", "content": blocks}))

    async with opened(gate, bearer) as session:
        invoked = await session.invoke(SPEAK_TOOL, speak_wire())

    assert invoked == ToolOutcome("seen", is_error=False)


async def test_an_invoked_tool_the_app_refuses_is_a_failure(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    gate.script(CALL, lambda reply: reply.status(HTTPStatus.SERVICE_UNAVAILABLE))

    async with opened(gate, bearer) as session:
        with pytest.raises(StatusFailure) as failure:
            await session.invoke(SPEAK_TOOL, speak_wire())

    assert failure.value.status == HTTPStatus.SERVICE_UNAVAILABLE


async def refused_as(session: MemberSession, name: str) -> HTTPStatus | bool:
    """The status the app answered when ``invoke`` raised, else whether the outcome is an error."""
    try:
        outcome = await session.invoke(name, {})
    except StatusFailure as failure:
        return failure.status
    return outcome.is_error


@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        pytest.param("tool-the-app-never-declared", HTTPStatus.BAD_REQUEST, id="undeclared"),
        pytest.param(SPEAK_TOOL, True, id="declared-but-not-offered"),
    ],
)
async def test_an_invoked_tool_outside_the_offer_is_a_status_failure_only_when_undeclared(
    app: MemberApp,
    changes: ResourceChanges,
    tools: MemberTools,
    caplog: pytest.LogCaptureFixture,
    name: str,
    refusal: HTTPStatus | bool,
) -> None:
    async with served(
        app, caplog, tools=tools.served(), changes=changes, offered_tools=ToolOffer()
    ) as gate:
        subject_bearer = member_session_support.a_bearer_let_in(gate)
        async with opened(gate, subject_bearer) as session:
            refused = await refused_as(session, name)

    assert refused == refusal
    assert not tools.calls
