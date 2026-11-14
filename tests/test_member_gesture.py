"""What an agent and an app say about a gesture: the arguments, the answers and the declaration."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from agents.figure_vocabulary import FigureGesture
from agents.member import (
    GESTURE_TOOL,
    GestureArguments,
    GestureRefusal,
    GestureRefused,
    PlaceRef,
    Relayed,
    gesture_tool,
)
from agents.tools import ToolEffect
from tests.support.member_wire import gesture_wire

PLACE = PlaceRef(kind="room", id="r-1")


def keys_at_any_depth(node: Any) -> set[str]:
    if isinstance(node, list):
        return {key for item in node for key in keys_at_any_depth(item)}
    if not isinstance(node, dict):
        return set()
    return set(node) | {key for value in node.values() for key in keys_at_any_depth(value)}


@pytest.mark.parametrize("gesture", list(FigureGesture), ids=lambda gesture: gesture.value)
def test_gesture_arguments_read_each_gesture_of_figures_set_back_from_their_json(
    gesture: FigureGesture,
) -> None:
    wire = gesture_wire(gesture=gesture.value)

    arguments = GestureArguments.model_validate_json(json.dumps(wire))

    assert arguments == GestureArguments(place=PLACE, gesture=gesture)
    assert json.loads(arguments.model_dump_json()) == wire


@pytest.mark.parametrize(
    "fields",
    [
        {"gesture": "somersault"},
        {"gesture": "WAVE"},
        {"gesture": ""},
        {"gesture": 1},
        {"gesture": None},
        {"extra": 1},
    ],
    ids=["unknown-token", "other-case", "empty", "number", "null", "unknown-member"],
)
def test_gesture_arguments_with_a_gesture_outside_figures_set_are_refused(
    fields: dict[str, Any],
) -> None:
    wire_text = json.dumps(gesture_wire(**fields))

    with pytest.raises(ValidationError):
        GestureArguments.model_validate_json(wire_text)


def test_gesture_arguments_without_a_gesture_are_refused() -> None:
    wire = {key: value for key, value in gesture_wire().items() if key != "gesture"}

    with pytest.raises(ValidationError):
        GestureArguments.model_validate_json(json.dumps(wire))


def test_a_relayed_answer_is_the_marker_alone() -> None:
    assert json.loads(Relayed(relayed=True).model_dump_json()) == {"relayed": True}


@pytest.mark.parametrize(
    "wire",
    [{"relayed": False}, {"relayed": "yes"}, {}, {"relayed": True, "played": True}],
    ids=["false", "text", "empty", "extra"],
)
def test_a_relayed_answer_that_is_not_exactly_the_marker_is_refused(wire: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Relayed.model_validate_json(json.dumps(wire))


def test_a_refusal_reads_back_as_not_active() -> None:
    refused = GestureRefused.model_validate_json(json.dumps({"refused": "not_active"}))

    assert refused.refused is GestureRefusal.NOT_ACTIVE


def test_a_refusal_the_standard_does_not_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        GestureRefused.model_validate_json(json.dumps({"refused": "no_figure"}))


def test_the_declaration_names_the_tool_and_its_schema_offers_exactly_figures_gestures() -> None:
    declaration = gesture_tool()

    assert declaration.name == GESTURE_TOOL
    assert declaration.effect is ToolEffect.SIDE_EFFECTING
    gesture_schema = declaration.parameters["properties"]["gesture"]
    assert gesture_schema["enum"] == [gesture.value for gesture in FigureGesture]


def test_the_declaration_schema_holds_no_reference_definition_or_title() -> None:
    parameters = gesture_tool().parameters

    assert {"$ref", "$defs", "title"}.isdisjoint(keys_at_any_depth(parameters))
    assert set(parameters["properties"]) == {"place", "gesture"}
    assert set(parameters["properties"]["place"]["properties"]) == {"kind", "id"}
