"""The member session's tool calls against an app's front on a loopback socket: what it sends, how
it tells an answer from a refusal, and which optional argument it leaves out.

The tools are the library's own records served by the real front; a scripted gate answers where a
test needs the app to misbehave.
"""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from typing import Any

import pytest
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, ListToolsResult, Tool
from pydantic import SecretStr

from agents.member import (
    ACCEPT_INVITATION_TOOL,
    CONTINUE_SPEAKING_TOOL,
    DECLINE_INVITATION_TOOL,
    SET_TURN_STATE_TOOL,
    SPEAK_TOOL,
    STOP_SPEAKING_TOOL,
    AcceptInvitationArguments,
    AcceptInvitationRefusal,
    AcceptInvitationRefused,
    ContinueSpeakingRefusal,
    ContinueSpeakingRefused,
    DeclineInvitationArguments,
    DeclineInvitationReason,
    DeclineInvitationRefusal,
    DeclineInvitationRefused,
    FigureLook,
    FigureRef,
    HttpsFileRef,
    PlaceRef,
    SetTurnStateArguments,
    SetTurnStateRefusal,
    SetTurnStateRefused,
    SpeakRefusal,
    SpeakRefused,
    Spoken,
    StopSpeakingRefusal,
    StopSpeakingRefused,
    Written,
    member_arguments,
)
from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    ArgumentLeftOut,
    Delivery,
    StatusFailure,
    UnrecognisedRefusal,
    UnusableAnswer,
    jsonrpc_code,
)
from agents.tools import ToolOutcome
from tests.support import member_session_support
from tests.support.mcp_server_support import MemberApp
from tests.support.member_session_support import MemberTools, opened, served
from tests.support.scripted_gate import CALL, LIST_TOOLS, GateReply, ScriptedGate

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


def arguments_of(call: dict[str, Any], record: type[AcceptInvitationArguments]) -> Any:
    return member_arguments(record, call)


def a_figure() -> FigureRef:
    return FigureRef(url="https://figures.test/figure.glb", sha256=secrets.token_hex(32))


def accepting(place: PlaceRef, **fields: Any) -> AcceptInvitationArguments:
    return AcceptInvitationArguments(place=place, invitation="inv-3", epoch=2, **fields)


def refusing(tools: MemberTools, tool: str, text: str, *, is_error: bool = True) -> None:
    tools.outcomes.setdefault(tool, []).append(ToolOutcome(text, is_error))


# Accepting and declining an invitation.


async def test_an_accept_sends_the_place_invitation_and_epoch_and_a_success_is_no_refusal(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        refusal = await session.accept(accepting(place))

    assert refusal is None
    assert [
        arguments_of(call, AcceptInvitationArguments)
        for call in tools.called(ACCEPT_INVITATION_TOOL)
    ] == [accepting(place)]


async def test_an_accept_sends_the_figure_it_carries_and_without_one_no_figure_key(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    figure = a_figure()

    async with opened(gate, bearer) as session:
        await session.accept(accepting(place, figure=figure))
        await session.accept(accepting(place))

    with_figure, without = tools.called(ACCEPT_INVITATION_TOOL)
    assert with_figure["figure"] == {"url": figure.url, "sha256": figure.sha256}
    assert "figure" not in without


@pytest.mark.parametrize("refusal", list(AcceptInvitationRefusal))
async def test_an_accept_refused_with_a_code_of_the_record_is_told_by_that_code(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    refusal: AcceptInvitationRefusal,
) -> None:
    refusing(
        tools, ACCEPT_INVITATION_TOOL, AcceptInvitationRefused(refused=refusal).model_dump_json()
    )

    async with opened(gate, bearer) as session:
        assert await session.accept(accepting(place)) is refusal


@pytest.mark.parametrize("text", ['{"refused": "own_account_only"}', "something else", "busy"])
async def test_an_accept_error_outside_the_record_is_unrecognised(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef, text: str
) -> None:
    refusing(tools, ACCEPT_INVITATION_TOOL, text)

    async with opened(gate, bearer) as session:
        assert await session.accept(accepting(place)) is UnrecognisedRefusal.UNRECOGNISED


def declining(place: PlaceRef) -> DeclineInvitationArguments:
    return DeclineInvitationArguments(
        place=place, invitation="inv-3", epoch=2, reason=DeclineInvitationReason.BUSY
    )


async def test_a_decline_sends_the_place_invitation_epoch_and_reason_and_is_sent(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        outcome = await session.decline(declining(place))

    assert outcome is Delivery.SENT
    assert [
        member_arguments(DeclineInvitationArguments, call)
        for call in tools.called(DECLINE_INVITATION_TOOL)
    ] == [declining(place)]


@pytest.mark.parametrize("refusal", list(DeclineInvitationRefusal))
async def test_a_decline_refused_with_a_code_of_the_record_is_told_by_that_code(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    refusal: DeclineInvitationRefusal,
) -> None:
    refusing(
        tools, DECLINE_INVITATION_TOOL, DeclineInvitationRefused(refused=refusal).model_dump_json()
    )

    async with opened(gate, bearer) as session:
        assert await session.decline(declining(place)) is refusal


@pytest.mark.parametrize("text", ['{"refused": "other"}', "something else"])
async def test_a_decline_error_outside_the_record_is_unrecognised(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef, text: str
) -> None:
    refusing(tools, DECLINE_INVITATION_TOOL, text)

    async with opened(gate, bearer) as session:
        assert await session.decline(declining(place)) is UnrecognisedRefusal.UNRECOGNISED


@pytest.mark.parametrize(
    "tools", [{"withheld": frozenset({DECLINE_INVITATION_TOOL})}], indirect=True
)
async def test_a_decline_of_an_app_that_lists_no_tool_to_decline_with_is_left_to_run_out(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        outcome = await session.decline(declining(place))

    assert outcome is Delivery.NOT_SERVED
    assert tools.calls == []


async def test_an_accept_on_a_cold_session_fails_with_the_failure_of_the_apps_tools_list(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async def refuses_the_list(reply: GateReply) -> None:
        await reply.error(INTERNAL_ERROR, status=HTTPStatus.OK)

    gate.script(LIST_TOOLS, refuses_the_list)
    invitation = accepting(place, figure=a_figure())

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.accept(invitation)

    assert failure.value.code == jsonrpc_code(INTERNAL_ERROR)
    assert tools.calls == []


async def test_an_accept_without_the_tool_is_a_protocol_error_that_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    async def knows_no_such_tool(reply: GateReply) -> None:
        await reply.error(INVALID_PARAMS, status=HTTPStatus.OK)

    gate.script(CALL, knows_no_such_tool)
    invitation = accepting(place)

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.accept(invitation)

    assert failure.value.code == jsonrpc_code(INVALID_PARAMS)
    assert (failure.value.method, failure.value.answer) == (None, None)


@pytest.mark.parametrize(
    "answer",
    [
        {"content": 7},
        {"resultType": "complete", "content": [], "structuredContent": {"standings": 7}},
        {"resultType": "complete", "content": []},
        {"resultType": "input_required", "requestState": "x"},
    ],
    ids=[
        "malformed result",
        "structured content against the output schema",
        "structured content missing",
        "input required",
    ],
)
async def test_an_accept_result_the_sdk_cannot_validate_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr, place: PlaceRef, answer: dict[str, Any]
) -> None:
    schema = {
        "type": "object",
        "properties": {"standings": {"type": "array"}},
        "required": ["standings"],
    }

    async def lists_a_tool_with_an_output_schema(reply: GateReply) -> None:
        tool = Tool(
            name=ACCEPT_INVITATION_TOOL,
            description="Accepts an invitation.",
            input_schema={"type": "object"},
            output_schema=schema,
        )
        await reply.result(ListToolsResult(tools=[tool]))

    gate.script(LIST_TOOLS, lists_a_tool_with_an_output_schema)
    gate.script(CALL, lambda reply: reply.raw_result(answer))
    invitation = accepting(place, figure=a_figure())

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.accept(invitation)

    assert (failure.value.code, gate.count(CALL)) == (INVALID_ANSWER_CODE, 1)


# Speaking.


async def test_a_speak_sends_the_place_and_the_sentence_and_gives_that_it_starts_to_play(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    sentence = secrets.token_hex(4)

    async with opened(gate, bearer) as session:
        spoken = await session.speak(place, sentence)

    assert spoken == Spoken(spoken=True)
    [call] = tools.called(SPEAK_TOOL)
    assert (call["place"], call["text"]) == (place.model_dump(mode="json"), sentence)


async def test_a_speak_the_app_answers_as_written_to_the_place_instead_is_told_as_written(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    refusing(tools, SPEAK_TOOL, Written(written=True).model_dump_json(), is_error=False)

    async with opened(gate, bearer) as session:
        answer = await session.speak(place, "A sentence.")

    assert answer == Written(written=True)


@pytest.mark.parametrize("refusal", list(SpeakRefusal))
async def test_a_speak_refused_with_a_code_of_the_record_is_told_by_that_code(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    refusal: SpeakRefusal,
) -> None:
    refusing(tools, SPEAK_TOOL, SpeakRefused(refused=refusal).model_dump_json())

    async with opened(gate, bearer) as session:
        assert await session.speak(place, "A sentence.") is refusal


@pytest.mark.parametrize(
    "outcome",
    [
        ToolOutcome("not json", is_error=False),
        ToolOutcome('{"spoken": true, "duration_ms": 1500}', is_error=False),
        ToolOutcome(SpeakRefused(refused=SpeakRefusal.TOO_LONG).model_dump_json(), is_error=False),
        ToolOutcome('{"refused": "other"}', is_error=True),
        ToolOutcome(Spoken(spoken=True).model_dump_json(), is_error=True),
        ToolOutcome('{"written": false}', is_error=False),
        ToolOutcome(Written(written=True).model_dump_json(), is_error=True),
    ],
    ids=[
        "no json",
        "a length from before",
        "a refusal that succeeded",
        "an unknown refusal",
        "a start that failed",
        "a sentence not written",
        "a written sentence that failed",
    ],
)
async def test_a_speak_answer_outside_the_records_is_an_unusable_answer(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    outcome: ToolOutcome,
) -> None:
    tools.outcomes[SPEAK_TOOL] = [outcome]

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.speak(place, "A sentence.")

    assert failure.value.code == INVALID_ANSWER_CODE


async def test_a_speak_the_app_refuses_is_a_failure(
    gate: ScriptedGate, bearer: SecretStr, place: PlaceRef
) -> None:
    gate.script(CALL, lambda reply: reply.status(HTTPStatus.SERVICE_UNAVAILABLE))

    async with opened(gate, bearer) as session:
        with pytest.raises(StatusFailure) as failure:
            await session.speak(place, "A sentence.")

    assert failure.value.status == HTTPStatus.SERVICE_UNAVAILABLE


# Deciding what becomes of the sentence a person talked over.


@pytest.mark.parametrize("tool", [CONTINUE_SPEAKING_TOOL, STOP_SPEAKING_TOOL])
async def test_a_decision_on_a_hold_that_stands_is_sent_with_the_place_and_the_hold_and_gives_none(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef, tool: str
) -> None:
    async with opened(gate, bearer) as session:
        decide = (
            session.continue_speaking if tool == CONTINUE_SPEAKING_TOOL else session.stop_speaking
        )
        assert await decide(place, "hold-1") is None

    [call] = tools.called(tool)
    assert (call["place"], call["interrupted"]) == (place.model_dump(mode="json"), "hold-1")


async def test_a_continue_with_finish_goes_to_an_app_that_declares_it_with_the_finish(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        assert await session.continue_speaking(place, "hold-1", finish=True) is None
        assert await session.continue_speaking(place, "hold-2", finish=True) is None

    assert [call["finish"] for call in tools.called(CONTINUE_SPEAKING_TOOL)] == [True, True]
    assert gate.count(LIST_TOOLS) == 1


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param({"undeclared": frozenset({"finish"})}, id="a-continue-declaring-no-finish"),
        pytest.param(
            {"withheld": frozenset({CONTINUE_SPEAKING_TOOL})}, id="an-app-without-the-continue"
        ),
    ],
    indirect=True,
)
async def test_a_finish_to_an_app_whose_continue_declares_none_sends_nothing_and_is_not_served(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        assert await session.continue_speaking(place, "hold-1", finish=True) is Delivery.NOT_SERVED

    assert tools.calls == []


@pytest.mark.parametrize("tools", [{"undeclared": frozenset({"keeps_queue"})}], indirect=True)
async def test_keeps_queue_lists_first_in_a_cold_session_and_is_left_out_of_an_app_declaring_none(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    left_out: list[ArgumentLeftOut] = []

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        await session.speak(place, "first", interrupted="hold-1", keeps_queue=True)
        await session.speak(place, "second", interrupted="hold-2", keeps_queue=True)

    assert [call.get("keeps_queue") for call in tools.called(SPEAK_TOOL)] == [None, None]
    assert [call["interrupted"] for call in tools.called(SPEAK_TOOL)] == ["hold-1", "hold-2"]
    assert gate.count(LIST_TOOLS) == 1
    assert left_out == [ArgumentLeftOut(SPEAK_TOOL, "keeps_queue")]


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param({}, id="an-app-declaring-keeps-queue"),
        pytest.param({"undeclared": frozenset({"keeps_queue"})}, id="an-app-declaring-none"),
    ],
    indirect=True,
)
async def test_keeps_queue_without_the_hold_is_refused_before_anything_is_sent(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        with pytest.raises(ValueError, match="keeps_queue"):
            await session.speak(place, "first", keeps_queue=True)

    assert tools.calls == []
    assert gate.count(LIST_TOOLS) == 0


async def test_keeps_queue_goes_to_an_app_that_declares_it(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    left_out: list[ArgumentLeftOut] = []

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        await session.speak(place, "first", interrupted="hold-1", keeps_queue=True)

    [call] = tools.called(SPEAK_TOOL)
    assert (call["interrupted"], call["keeps_queue"]) == ("hold-1", True)
    assert left_out == []


@pytest.mark.parametrize(
    ("tool", "refusal"),
    [
        (
            CONTINUE_SPEAKING_TOOL,
            ContinueSpeakingRefused(refused=ContinueSpeakingRefusal.NOT_INTERRUPTED),
        ),
        (STOP_SPEAKING_TOOL, StopSpeakingRefused(refused=StopSpeakingRefusal.NOT_INTERRUPTED)),
    ],
)
async def test_a_decision_on_a_hold_that_does_not_stand_is_told_by_the_apps_refusal(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    tool: str,
    refusal: ContinueSpeakingRefused | StopSpeakingRefused,
) -> None:
    refusing(tools, tool, refusal.model_dump_json())

    async with opened(gate, bearer) as session:
        decide = (
            session.continue_speaking if tool == CONTINUE_SPEAKING_TOOL else session.stop_speaking
        )
        assert await decide(place, "hold-1") is refusal.refused


@pytest.mark.parametrize("tool", [CONTINUE_SPEAKING_TOOL, STOP_SPEAKING_TOOL])
async def test_a_refusal_of_a_decision_that_is_none_of_the_records_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef, tool: str
) -> None:
    refusing(tools, tool, '{"refused": "other"}')

    async with opened(gate, bearer) as session:
        decide = (
            session.continue_speaking if tool == CONTINUE_SPEAKING_TOOL else session.stop_speaking
        )
        with pytest.raises(UnusableAnswer) as failure:
            await decide(place, "hold-1")

    assert failure.value.code == INVALID_ANSWER_CODE


# Stating what the member's turns do.


async def test_a_statement_is_sent_whole_and_a_taken_one_gives_no_refusal(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    statement = SetTurnStateArguments(place=place, thinking=True)

    async with opened(gate, bearer) as session:
        assert await session.set_turn_state(statement) is None

    assert [
        member_arguments(SetTurnStateArguments, call) for call in tools.called(SET_TURN_STATE_TOOL)
    ] == [statement]


async def test_a_statement_the_app_refuses_is_told_by_its_refusal(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    refused = SetTurnStateRefused(refused=SetTurnStateRefusal.NOT_HELD)
    refusing(tools, SET_TURN_STATE_TOOL, refused.model_dump_json())

    async with opened(gate, bearer) as session:
        assert await session.set_turn_state(SetTurnStateArguments(place=place)) is refused.refused


async def test_a_statement_refusal_that_is_none_of_the_records_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    refusing(tools, SET_TURN_STATE_TOOL, "not json")

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.set_turn_state(SetTurnStateArguments(place=place))

    assert failure.value.code == INVALID_ANSWER_CODE


# An optional argument the app's tool does not declare.

THAT_PREDATE_THE_ARGUMENTS = {
    "undeclared": frozenset({"figure", "voices", "style", "interrupted", "thinking"})
}


@pytest.mark.parametrize("tools", [THAT_PREDATE_THE_ARGUMENTS], indirect=True)
async def test_undeclared_optional_arguments_are_left_out_and_each_is_reported_once_per_session(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    left_out: list[ArgumentLeftOut] = []
    accept = accepting(place, figure=a_figure(), voices=("amy",))

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        assert await session.accept(accept) is None
        assert await session.accept(accept) is None
        await session.speak(place, "hello", style="calm")
        await session.speak(place, "again", interrupted="hold-1")
        await session.speak(place, "once more", interrupted="hold-2")
        await session.set_turn_state(SetTurnStateArguments(place=place, thinking=True))

    assert (
        tools.called(ACCEPT_INVITATION_TOOL)
        == [{"place": place.model_dump(mode="json"), "invitation": "inv-3", "epoch": 2}] * 2
    )
    assert [sorted(call) for call in tools.called(SPEAK_TOOL)] == [["place", "text"]] * 3
    assert left_out == [
        ArgumentLeftOut(ACCEPT_INVITATION_TOOL, "figure"),
        ArgumentLeftOut(ACCEPT_INVITATION_TOOL, "voices"),
        ArgumentLeftOut(SPEAK_TOOL, "style"),
        ArgumentLeftOut(SPEAK_TOOL, "interrupted"),
        ArgumentLeftOut(SET_TURN_STATE_TOOL, "thinking"),
    ]


async def test_optional_arguments_an_app_declares_are_sent_and_reported_as_none(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    left_out: list[ArgumentLeftOut] = []
    figure = a_figure()

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        await session.accept(accepting(place, figure=figure, voices=("amy",)))
        await session.speak(place, "hello", style="calm")

    [sent] = tools.called(ACCEPT_INVITATION_TOOL)
    assert sent["voices"] == ["amy"]
    assert sent["figure"]["url"] == figure.url
    assert [call.get("style") for call in tools.called(SPEAK_TOOL)] == ["calm"]
    assert left_out == []


def a_look_with_a_room() -> tuple[FigureLook, HttpsFileRef]:
    room = HttpsFileRef(url="https://figures.test/figure.room.glb", sha256=secrets.token_hex(32))
    look = FigureLook(
        kind="figure",
        url="https://figures.test/figure.vrm",
        sha256=secrets.token_hex(32),
        room=room,
    )
    return look, room


async def test_an_app_that_declares_the_room_of_a_look_gets_the_look_with_it(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    look, room = a_look_with_a_room()
    left_out: list[ArgumentLeftOut] = []

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        await session.accept(accepting(place, looks=(look,)))

    [sent] = tools.called(ACCEPT_INVITATION_TOOL)
    assert sent["looks"] == [
        {
            "kind": "figure",
            "url": look.url,
            "sha256": look.sha256,
            "room": {"url": room.url, "sha256": room.sha256},
        }
    ]
    assert left_out == []


@pytest.mark.parametrize(
    ("tools", "looks_kept"),
    [
        pytest.param({"undeclared": frozenset({"looks"})}, False, id="no looks"),
        pytest.param({"undeclared": frozenset({"looks[].room"})}, True, id="no room of a look"),
    ],
    indirect=["tools"],
)
async def test_an_app_without_looks_or_a_look_room_gets_the_accept_without_it_and_it_is_reported(
    gate: ScriptedGate,
    bearer: SecretStr,
    tools: MemberTools,
    place: PlaceRef,
    looks_kept: bool,
) -> None:
    look, _ = a_look_with_a_room()
    undeclared = next(iter(tools.undeclared))
    left_out: list[ArgumentLeftOut] = []

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        assert await session.accept(accepting(place, looks=(look,))) is None
        assert await session.accept(accepting(place, looks=(look,))) is None

    for sent in tools.called(ACCEPT_INVITATION_TOOL):
        assert sent.get("looks") == (
            [{"kind": "figure", "url": look.url, "sha256": look.sha256}] if looks_kept else None
        )
    assert left_out == [ArgumentLeftOut(ACCEPT_INVITATION_TOOL, undeclared)]


@pytest.mark.parametrize("tools", [{"undeclared": frozenset({"figure"})}], indirect=True)
async def test_a_required_argument_is_never_left_out_whatever_the_app_declares(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef
) -> None:
    async with opened(gate, bearer) as session:
        await session.accept(accepting(place, figure=a_figure()))

    [sent] = tools.called(ACCEPT_INVITATION_TOOL)
    assert {"place", "invitation", "epoch"} <= set(json.loads(json.dumps(sent)))


@pytest.mark.parametrize(
    ("tool", "call"),
    [
        pytest.param(
            CONTINUE_SPEAKING_TOOL,
            lambda session, place: session.continue_speaking(place, "hold-1", told_changes=3),
            id="continue_speaking",
        ),
        pytest.param(
            STOP_SPEAKING_TOOL,
            lambda session, place: session.stop_speaking(place, "hold-1", told_changes=3),
            id="stop_speaking",
        ),
        pytest.param(
            SPEAK_TOOL,
            lambda session, place: session.speak(
                place, "Ja?", interrupted="hold-1", told_changes=3
            ),
            id="speak",
        ),
    ],
)
async def test_told_changes_goes_to_an_app_that_declares_it_without_listing_the_tools_first(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef, tool: str, call: Any
) -> None:
    async with opened(gate, bearer) as session:
        await call(session, place)

    [sent] = tools.called(tool)
    assert sent["told_changes"] == 3
    assert gate.count(LIST_TOOLS) == 0


@pytest.mark.parametrize("tools", [{"undeclared": frozenset({"told_changes"})}], indirect=True)
@pytest.mark.parametrize(
    ("tool", "call"),
    [
        pytest.param(
            CONTINUE_SPEAKING_TOOL,
            lambda session, place: session.continue_speaking(place, "hold-1", told_changes=3),
            id="continue_speaking",
        ),
        pytest.param(
            STOP_SPEAKING_TOOL,
            lambda session, place: session.stop_speaking(place, "hold-1", told_changes=3),
            id="stop_speaking",
        ),
        pytest.param(
            SPEAK_TOOL,
            lambda session, place: session.speak(
                place, "Ja?", interrupted="hold-1", told_changes=3
            ),
            id="speak",
        ),
    ],
)
async def test_told_changes_is_left_out_of_an_app_declaring_none_and_reported_once(
    gate: ScriptedGate, bearer: SecretStr, tools: MemberTools, place: PlaceRef, tool: str, call: Any
) -> None:
    left_out: list[ArgumentLeftOut] = []

    async with opened(gate, bearer, on_argument_left_out=left_out.append) as session:
        await session.declared_tools()
        await call(session, place)
        await call(session, place)

    assert [sent.get("told_changes") for sent in tools.called(tool)] == [None, None]
    assert left_out == [ArgumentLeftOut(tool, "told_changes")]
