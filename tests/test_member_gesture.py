"""What an agent and an app say about a gesture: the arguments, the answers and the declaration."""

from __future__ import annotations

import json
from typing import Any

import pytest

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
from tests.support.member_wire import gesture_wire, is_refused, keys_at_any_depth

PLACE = PlaceRef(kind="room", id="r-1")


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
    ],
    ids=["unknown-token", "other-case", "empty", "number", "null"],
)
def test_gesture_arguments_with_a_gesture_outside_figures_set_are_refused(
    fields: dict[str, Any],
) -> None:
    assert is_refused(GestureArguments, gesture_wire(**fields))


def test_gesture_arguments_with_a_member_of_their_own_making_are_refused() -> None:
    assert is_refused(GestureArguments, gesture_wire(extra=1))


def test_gesture_arguments_without_a_gesture_are_refused() -> None:
    wire = {key: value for key, value in gesture_wire().items() if key != "gesture"}

    assert is_refused(GestureArguments, wire)


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
