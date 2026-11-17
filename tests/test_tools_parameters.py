"""A record becomes a tool's input schema with its references inlined and no title."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field

from agents.tools import tool_parameters, untitled_schema
from tests.support.member_wire import keys_at_any_depth


class Colour(StrEnum):
    RED = "red"
    BLUE = "blue"


class Spot(BaseModel):
    """Where a thing is."""

    row: int
    column: int


class Brush(BaseModel):
    spot: Spot
    colour: Colour
    title: str
    note: str | None = Field(default=None, description="What to remember.")


class Round(BaseModel):
    kind: Literal["round"]
    radius: int


class Square(BaseModel):
    kind: Literal["square"]
    side: int


class Shape(BaseModel):
    outline: Annotated[Round | Square, Field(discriminator="kind")]


class Tree(BaseModel):
    branches: list[Tree]


def test_references_are_inlined_and_no_definitions_or_reference_is_left() -> None:
    parameters = tool_parameters(Brush)

    assert {"$defs", "$ref"}.isdisjoint(keys_at_any_depth(parameters))
    assert parameters["properties"]["spot"]["properties"]["row"]["type"] == "integer"
    assert parameters["properties"]["colour"]["enum"] == [colour.value for colour in Colour]


def test_no_title_is_left_but_a_property_named_title_and_the_descriptions_stay() -> None:
    parameters = tool_parameters(Brush)

    assert parameters["properties"]["title"] == {"type": "string"}
    assert parameters["properties"]["note"]["description"] == "What to remember."
    assert parameters["properties"]["spot"]["description"] == "Where a thing is."
    assert all("title" not in schema for schema in parameters["properties"].values())
    assert "title" not in parameters


def test_a_discriminated_union_keeps_its_variants_and_leaves_no_unresolved_reference() -> None:
    parameters = tool_parameters(Shape)

    outline = parameters["properties"]["outline"]
    assert [variant["properties"]["kind"]["const"] for variant in outline["oneOf"]] == [
        "round",
        "square",
    ]
    assert "discriminator" not in keys_at_any_depth(parameters)
    assert "#/$defs/" not in json.dumps(parameters)


def test_a_recursive_record_is_refused() -> None:
    with pytest.raises(ValueError, match="recursive"):
        tool_parameters(Tree)


def test_untitled_schema_is_public_and_drops_title_and_discriminator_but_keeps_properties() -> None:
    schema = {
        "title": "Brush",
        "discriminator": {"propertyName": "kind"},
        "properties": {"title": {"title": "Title", "type": "string"}, "discriminator": {}},
    }

    assert untitled_schema(schema) == {
        "properties": {"title": {"type": "string"}, "discriminator": {}}
    }
