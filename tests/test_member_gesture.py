"""What an agent and an app say about a gesture and a framing: the arguments, the answers and the
declaration."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel

from agents.figure_vocabulary import FigureFraming, FigureGesture
from agents.member import (
    GESTURE_TOOL,
    MEMBER_TOOL_ARGUMENTS,
    MEMBER_TOOL_RESULTS,
    SET_FRAMING_TOOL,
    GestureArguments,
    GestureRefusal,
    GestureRefused,
    PlaceRef,
    Relayed,
    SetFramingArguments,
    SetFramingRefusal,
    SetFramingRefused,
    framing_tool,
    gesture_tool,
)
from agents.tools import ToolDeclaration, ToolEffect
from tests.support.member_wire import framing_wire, gesture_wire, is_refused, keys_at_any_depth

PLACE = PlaceRef(kind="room", id="r-1")


@dataclass(frozen=True)
class FigureAct:
    """A tool by which an agent chooses what its figure shows: its field and figure's set."""

    tool: str
    field: str
    tokens: type[StrEnum]
    arguments: type[BaseModel]
    wire: Callable[..., dict[str, object]]
    declaration: Callable[[], ToolDeclaration]


GESTURE = FigureAct(
    GESTURE_TOOL, "gesture", FigureGesture, GestureArguments, gesture_wire, gesture_tool
)
FRAMING = FigureAct(
    SET_FRAMING_TOOL, "framing", FigureFraming, SetFramingArguments, framing_wire, framing_tool
)
ACTS = pytest.mark.parametrize("act", [GESTURE, FRAMING], ids=lambda act: act.field)


def figure_tokens() -> list[Any]:
    return [
        pytest.param(act, token, id=token.value)
        for act in (GESTURE, FRAMING)
        for token in act.tokens
    ]


@pytest.mark.parametrize(("act", "token"), figure_tokens())
def test_arguments_read_each_token_of_figures_set_back_from_their_json(
    act: FigureAct, token: StrEnum
) -> None:
    wire = act.wire(**{act.field: token.value})

    arguments = act.arguments.model_validate_json(json.dumps(wire))

    assert arguments == act.arguments(place=PLACE, **{act.field: token})
    assert json.loads(arguments.model_dump_json()) == wire


@ACTS
@pytest.mark.parametrize(
    "token",
    ["somersault", "WAVE", "FACE", "full_body", "", 1, None],
    ids=["unknown", "other-case", "upper-case", "snake-case", "empty", "number", "null"],
)
def test_arguments_with_a_token_outside_figures_set_are_refused(act: FigureAct, token: Any) -> None:
    assert is_refused(act.arguments, act.wire(**{act.field: token}))


@ACTS
def test_arguments_with_a_member_of_their_own_making_are_refused(act: FigureAct) -> None:
    assert is_refused(act.arguments, act.wire(extra=1))


@ACTS
def test_arguments_without_the_token_are_refused(act: FigureAct) -> None:
    wire = {key: value for key, value in act.wire().items() if key != act.field}

    assert is_refused(act.arguments, wire)


def test_a_relayed_answer_is_the_marker_alone() -> None:
    assert json.loads(Relayed(relayed=True).model_dump_json()) == {"relayed": True}


@pytest.mark.parametrize(
    "wire",
    [{"relayed": False}, {"relayed": "yes"}, {}, {"relayed": True, "played": True}],
    ids=["false", "text", "empty", "extra"],
)
def test_a_relayed_answer_that_is_not_exactly_the_marker_is_refused(wire: dict[str, Any]) -> None:
    assert is_refused(Relayed, wire)


def test_a_refusal_reads_back_as_not_active() -> None:
    refused = GestureRefused.model_validate_json(json.dumps({"refused": "not_active"}))

    assert refused.refused is GestureRefusal.NOT_ACTIVE


def test_a_refusal_the_standard_does_not_name_is_refused() -> None:
    assert is_refused(GestureRefused, {"refused": "no_figure"})


@ACTS
def test_the_declaration_names_the_tool_and_its_schema_offers_exactly_figures_tokens(
    act: FigureAct,
) -> None:
    declaration = act.declaration()

    assert declaration.name == act.tool
    assert declaration.effect is ToolEffect.SIDE_EFFECTING
    token_schema = declaration.parameters["properties"][act.field]
    assert token_schema["enum"] == [token.value for token in act.tokens]


@ACTS
def test_the_declaration_schema_holds_no_reference_definition_or_title(act: FigureAct) -> None:
    parameters = act.declaration().parameters

    assert {"$ref", "$defs", "title"}.isdisjoint(keys_at_any_depth(parameters))
    assert set(parameters["properties"]) == {"place", act.field}
    assert set(parameters["properties"]["place"]["properties"]) == {"kind", "id"}


def test_a_framing_refusal_reads_back_as_not_active() -> None:
    refused = SetFramingRefused.model_validate_json(json.dumps({"refused": "not_active"}))

    assert refused.refused is SetFramingRefusal.NOT_ACTIVE


def test_a_framing_refusal_the_standard_does_not_name_is_refused() -> None:
    assert is_refused(SetFramingRefused, {"refused": "no_figure"})


def test_the_framing_tool_lists_its_arguments_and_only_its_refusal_as_an_answer_record() -> None:
    assert MEMBER_TOOL_ARGUMENTS[SET_FRAMING_TOOL] is SetFramingArguments
    assert MEMBER_TOOL_RESULTS[SET_FRAMING_TOOL] == (SetFramingRefused,)
