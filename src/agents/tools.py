"""The tool contract an app declares and every agent layer reads.

This module is the one owner of ``ToolDeclaration`` and its ``ToolEffect``. ``HandTool`` is a
declaration with the handler an app serves it with, for the person a turn acts for;
``ToolOutcome`` is what a handler answers. ``Principal``, ``JsonObject`` and ``ToolImage`` are
re-exported so an app's tools import only this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agents.turn import JsonObject, Principal, ToolImage

__all__ = [
    "SCHEMA_ANNOTATIONS",
    "HandTool",
    "HandToolHandler",
    "JsonObject",
    "Principal",
    "ToolDeclaration",
    "ToolEffect",
    "ToolImage",
    "ToolOutcome",
    "agent_tool",
    "inlined_schema",
    "not_an_agent_tool",
    "untitled_schema",
    "tool_parameters",
]

AGENT_TOOL_EXTENSION: Final = "x-agent-tool"
SCHEMA_ANNOTATIONS: Final = frozenset({"title", "description", "default", "examples", "deprecated"})
_SCHEMA_VALUED: Final = frozenset(
    {
        "items",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "not",
        "contains",
        "if",
        "then",
        "else",
        "contentSchema",
    }
)
_SCHEMA_DROPPED: Final = frozenset({"title", "discriminator"})
_SCHEMA_MAPPED: Final = frozenset({"properties", "patternProperties", "dependentSchemas"})
_SCHEMA_LISTED: Final = frozenset({"anyOf", "allOf", "oneOf", "prefixItems"})
_RECORD_DEFINITIONS: Final = "#/$defs/"


class ToolEffect(StrEnum):
    """Whether running a tool can change state outside the run, as the MCP person front hints it
    to a client and checks it against the route's HTTP method."""

    READ_ONLY = "read_only"
    SIDE_EFFECTING = "side_effecting"


class ToolDeclaration(BaseModel):
    """One tool as a model sees it: its name, its purpose, its arguments.

    ``effect`` is for the host, not the model: the gateway projection picks its fields explicitly,
    so it never reaches a prompt. It defaults to side-effecting because a tool nobody classified
    must be assumed to change state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parameters: dict[str, Any]
    effect: ToolEffect = ToolEffect.SIDE_EFFECTING


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool answers: the text a model is shown, whether the call failed and, for a tool that
    lets the agent see, the one picture the model reads next to the text.
    """

    content: str
    is_error: bool
    image: ToolImage | None = None


type HandToolHandler = Callable[[Principal, JsonObject], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class HandTool:
    """A tool the app serves itself, for the person a turn acts for."""

    declaration: ToolDeclaration
    handler: HandToolHandler


_NAME_KEY: Final = "name"
_EFFECT_KEY: Final = "effect"
_EXCLUDED_KEY: Final = "excluded"


@dataclass(frozen=True)
class ToolMark:
    """What ``agent_tool`` wrote: the tool's name and its effect class."""

    name: str
    effect: ToolEffect


@dataclass(frozen=True)
class ToolExclusion:
    """What ``not_an_agent_tool`` wrote: the reason a route is no tool."""

    reason: str


def agent_tool(*, name: str, effect: ToolEffect) -> dict[str, object]:
    """The mark that makes a route a tool: pass it as FastAPI's ``openapi_extra``.

    A route becomes a tool only through its own mark; the other decision is
    ``not_an_agent_tool``. The client imports no FastAPI.
    """
    return {AGENT_TOOL_EXTENSION: {_NAME_KEY: name, _EFFECT_KEY: effect.value}}


def not_an_agent_tool(*, reason: str) -> dict[str, object]:
    """The exclusion of a route, with its reason: pass it as FastAPI's ``openapi_extra``.

    An excluded route yields no tool and no mark. The generator reads the same extension key as
    for ``agent_tool``, so one route cannot hold both decisions, and it validates the reason.
    """
    return {AGENT_TOOL_EXTENSION: {_EXCLUDED_KEY: reason}}


def read_decision(extension: JsonValue) -> ToolMark | ToolExclusion | None:
    """The decision an ``x-agent-tool`` value holds, or ``None`` when it is neither shape.

    Raises ``ValueError`` for an exclusion whose reason is not a non-blank string.
    """
    if not isinstance(extension, dict):
        return None
    if extension.keys() == {_EXCLUDED_KEY}:
        reason = extension[_EXCLUDED_KEY]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("an exclusion needs a reason that is a non-blank string")
        return ToolExclusion(reason)
    if extension.keys() == {_NAME_KEY, _EFFECT_KEY}:
        name, effect = extension[_NAME_KEY], extension[_EFFECT_KEY]
        if isinstance(name, str) and isinstance(effect, str) and effect in ToolEffect:
            return ToolMark(name, ToolEffect(effect))
    return None


def inlined_schema(
    node: JsonValue, definitions: JsonObject, prefix: str, seen: tuple[str, ...] = ()
) -> JsonValue:
    """The schema with every ``$ref`` that starts with ``prefix`` replaced by its definition.

    An annotation beside a reference wins over the definition's own. Raises ``ValueError`` for a
    recursive or unresolved reference (a non-local one included), a reference that is no string, a
    definition that is no object, or a keyword beside a reference that is no annotation.
    """
    if isinstance(node, list):
        return [inlined_schema(item, definitions, prefix, seen) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" not in node:
        return {
            key: inlined_schema(value, definitions, prefix, seen) for key, value in node.items()
        }
    reference = node["$ref"]
    if not isinstance(reference, str):
        raise ValueError("a $ref must be a string")
    name = reference.removeprefix(prefix)
    if name in seen or name not in definitions:
        raise ValueError("a schema is recursive or unresolved")
    resolved = inlined_schema(definitions[name], definitions, prefix, (*seen, name))
    if not isinstance(resolved, dict):
        raise ValueError("a referenced schema must be an object")
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if siblings.keys() - SCHEMA_ANNOTATIONS:
        raise ValueError("a reference may carry only an annotation beside it")
    return {**resolved, **siblings}


def untitled_schema(node: JsonValue) -> JsonValue:
    """The schema without ``title`` and the OpenAPI ``discriminator`` at any depth.

    A ``discriminator`` mapping holds references that no inlined schema resolves, and the ``const``
    of each variant already tells them apart. A property named ``title`` or ``discriminator`` and
    data stay.
    """
    if isinstance(node, list):
        return [untitled_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    kept: JsonObject = {}
    for key, value in node.items():
        if key in _SCHEMA_DROPPED:
            continue
        if key in _SCHEMA_MAPPED and isinstance(value, dict):
            kept[key] = {name: untitled_schema(schema) for name, schema in value.items()}
        elif key in _SCHEMA_VALUED or key in _SCHEMA_LISTED:
            kept[key] = untitled_schema(value)
        else:
            kept[key] = value
    return kept


def tool_parameters(record: type[BaseModel]) -> JsonObject:
    """The record as a tool's input schema: every local reference inlined, no ``$defs``, untitled.

    Untitled is ``untitled_schema``. A tool declared from a record takes its parameters from here,
    so the schema a model reads is the record's own. Raises ``ValueError`` for a recursive or
    non-local reference.
    """
    schema = cast("JsonObject", record.model_json_schema())
    definitions = cast("JsonObject", schema.pop("$defs", {}))
    return cast(
        "JsonObject", untitled_schema(inlined_schema(schema, definitions, _RECORD_DEFINITIONS))
    )
