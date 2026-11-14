"""A record becomes a tool's input schema with its references inlined and no title."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, Field

from agents.tools import tool_parameters


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


class Tree(BaseModel):
    branches: list[Tree]


def keys_at_any_depth(node: Any) -> set[str]:
    if isinstance(node, list):
        return {key for item in node for key in keys_at_any_depth(item)}
    if not isinstance(node, dict):
        return set()
    return set(node) | {key for value in node.values() for key in keys_at_any_depth(value)}


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


def test_a_recursive_record_is_refused() -> None:
    with pytest.raises(ValueError, match="recursive"):
        tool_parameters(Tree)
