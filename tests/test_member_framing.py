"""What an agent and an app say about a framing: the arguments, the refusal and the declaration."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.figure_vocabulary import FigureFraming
from agents.member import (
    MEMBER_TOOL_ARGUMENTS,
    MEMBER_TOOL_RESULTS,
    SET_FRAMING_TOOL,
    PlaceRef,
    SetFramingArguments,
    SetFramingRefusal,
    SetFramingRefused,
    framing_tool,
)
from agents.tools import ToolEffect
from tests.support.member_wire import framing_wire, is_refused, keys_at_any_depth

PLACE = PlaceRef(kind="room", id="r-1")


@pytest.mark.parametrize("framing", list(FigureFraming), ids=lambda framing: framing.value)
def test_framing_arguments_read_each_framing_of_figures_set_back_from_their_json(
    framing: FigureFraming,
) -> None:
    wire = framing_wire(framing=framing.value)

    arguments = SetFramingArguments.model_validate_json(json.dumps(wire))

    assert arguments == SetFramingArguments(place=PLACE, framing=framing)
    assert json.loads(arguments.model_dump_json()) == wire


@pytest.mark.parametrize(
    "fields",
    [
        {"framing": "closeup"},
        {"framing": "FACE"},
        {"framing": "full_body"},
        {"framing": ""},
        {"framing": 1},
        {"framing": None},
    ],
    ids=["unknown-token", "other-case", "snake-case-spelling", "empty", "number", "null"],
)
def test_framing_arguments_with_a_framing_outside_figures_set_are_refused(
    fields: dict[str, Any],
) -> None:
    assert is_refused(SetFramingArguments, framing_wire(**fields))


def test_framing_arguments_with_a_member_of_their_own_making_are_refused() -> None:
    assert is_refused(SetFramingArguments, framing_wire(extra=1))


def test_framing_arguments_without_a_framing_are_refused() -> None:
    wire = {key: value for key, value in framing_wire().items() if key != "framing"}

    assert is_refused(SetFramingArguments, wire)


def test_a_refusal_reads_back_as_not_active() -> None:
    refused = SetFramingRefused.model_validate_json(json.dumps({"refused": "not_active"}))

    assert refused.refused is SetFramingRefusal.NOT_ACTIVE


def test_a_refusal_the_standard_does_not_name_is_refused() -> None:
    assert is_refused(SetFramingRefused, {"refused": "no_figure"})


def test_the_tool_lists_its_arguments_and_only_its_refusal_as_an_answer_record() -> None:
    assert MEMBER_TOOL_ARGUMENTS[SET_FRAMING_TOOL] is SetFramingArguments
    assert MEMBER_TOOL_RESULTS[SET_FRAMING_TOOL] == (SetFramingRefused,)


def test_the_declaration_names_the_tool_and_its_schema_offers_exactly_figures_framings() -> None:
    declaration = framing_tool()

    assert declaration.name == SET_FRAMING_TOOL
    assert declaration.effect is ToolEffect.SIDE_EFFECTING
    framing_schema = declaration.parameters["properties"]["framing"]
    assert framing_schema["enum"] == [framing.value for framing in FigureFraming]


def test_the_declaration_schema_holds_no_reference_definition_or_title() -> None:
    parameters = framing_tool().parameters

    assert {"$ref", "$defs", "title"}.isdisjoint(keys_at_any_depth(parameters))
    assert set(parameters["properties"]) == {"place", "framing"}
    assert set(parameters["properties"]["place"]["properties"]) == {"kind", "id"}
