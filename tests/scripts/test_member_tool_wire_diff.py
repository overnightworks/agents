"""The release note line says whether the member-tool wire changed."""

from __future__ import annotations

import io
import json
import runpy
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import Field, StringConstraints, create_model

import scripts.member_tool_wire_diff as diff
from agents.turn import StrictModel

Tools = dict[str, dict[str, object]]

BASE: Tools = {
    "speak": {"arguments": {"a": 1}, "results": [{"r": 1}]},
    "gesture": {"arguments": {"b": 1}, "results": [{"r": 2}]},
}


def snapshot(tools: Tools, resources: dict[str, object] | None = None) -> str:
    document: dict[str, object] = {"version": 1, "tools": tools}
    if resources is not None:
        document["resources"] = resources
    return json.dumps(document)


@pytest.mark.parametrize(
    ("current", "line"),
    [
        (BASE, "**Member-tool wire:** unchanged."),
        (
            {**BASE, "speak": {"arguments": {"a": 2}, "results": [{"r": 1}]}},
            "**Member-tool wire:** changed: speak.",
        ),
        (
            {**BASE, "speak": {"arguments": {"a": 1}, "results": [{"r": 9}]}},
            "**Member-tool wire:** changed: speak.",
        ),
        (
            {**BASE, "stop_speaking": {"arguments": {}, "results": []}},
            "**Member-tool wire:** changed: stop_speaking.",
        ),
        (
            {"speak": BASE["speak"]},
            "**Member-tool wire:** changed: gesture.",
        ),
        (
            {"speak": {"arguments": {}, "results": []}, "new": BASE["gesture"]},
            "**Member-tool wire:** changed: gesture, new, speak.",
        ),
    ],
)
def test_the_line_names_the_tools_that_differ_from_the_previous_snapshot(
    current: Tools, line: str
) -> None:
    assert diff.note_line(snapshot(BASE), snapshot(current)) == line


Mood = StrEnum("Mood", {"CALM": "calm"})
MoodWithPanic = StrEnum("Mood", {"CALM": "calm", "PANIC": "panic"})


class Circle(StrictModel):
    tag: Literal["circle"] = "circle"


class Square(StrictModel):
    tag: Literal["square"] = "square"


class Triangle(StrictModel):
    tag: Literal["triangle"] = "triangle"


def record(name: str, doc: str | None = None, **fields: Any) -> type[StrictModel]:
    """A record named ``name``; each field is a ``(type, default)`` pair, ``...`` for no default."""
    return create_model(name, __base__=StrictModel, __doc__=doc, **fields)  # pyright: ignore[reportCallIssue, reportUnknownVariableType]


def resources_of(**records: type[StrictModel]) -> dict[str, object]:
    return {name: model.model_json_schema() for name, model in records.items()}


def wire_of(**records: type[StrictModel]) -> str:
    return snapshot(BASE, resources_of(**records))


def line_for(before: type[StrictModel], after: type[StrictModel]) -> str:
    return diff.note_line(wire_of(Alpha=before), wire_of(Alpha=after))


NAMED_DESCRIPTION = record("Named", description=(str, ...))
TITLE_PATTERN = StringConstraints(pattern="title")
PROPERTIES_PATTERN = StringConstraints(pattern="properties")

ADDED = "**Member-tool wire:** fields added: Alpha."
CHANGED = "**Member-tool wire:** changed: resources: Alpha."


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            record("Alpha", one=(int, ...)),
            record("Alpha", one=(int, ...), extra=(str | None, None)),
            id="top level",
        ),
        pytest.param(
            record("Alpha", inner=(record("Inner", one=(int, ...)), ...)),
            record("Alpha", inner=(record("Inner", one=(int, ...), extra=(str | None, None)), ...)),
            id="nested",
        ),
        pytest.param(
            record("Alpha", one=(int, ...)),
            record("Alpha", one=(int, ...), mood=(Mood | None, None)),
            id="with a new definition",
        ),
        pytest.param(
            record("Alpha", one=(int, ...)),
            record("Alpha", one=(int, ...), title=(str | None, None)),
            id="named title",
        ),
    ],
)
def test_an_added_optional_field_on_a_resource_record_is_named_as_added(
    before: type[StrictModel], after: type[StrictModel]
) -> None:
    assert line_for(before, after) == ADDED


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            record("Alpha", one=(int, ...), gone=(int | None, None)),
            record("Alpha", one=(int, ...)),
            id="removed",
        ),
        pytest.param(
            record("Alpha", one=(int, ...)),
            record("Alpha", other=(int, ...)),
            id="renamed",
        ),
        pytest.param(
            record("Alpha", one=(int, ...)), record("Alpha", one=(str, ...)), id="retyped"
        ),
        pytest.param(
            record("Alpha", one=(int, ...)),
            record("Alpha", one=(int, ...), extra=(str, ...)),
            id="newly required",
        ),
        pytest.param(
            record("Alpha", one=(int, 1)),
            record("Alpha", one=(int, 2)),
            id="changed default",
        ),
        pytest.param(
            record("Alpha", one=(Annotated[int, Field(ge=0)], ...)),
            record("Alpha", one=(Annotated[int, Field(ge=1)], ...)),
            id="changed bound",
        ),
        pytest.param(
            record("Alpha", shape=(Annotated[Circle | Square, Field(discriminator="tag")], ...)),
            record(
                "Alpha",
                shape=(Annotated[Circle | Square | Triangle, Field(discriminator="tag")], ...),
            ),
            id="new union variant",
        ),
        pytest.param(
            record("Alpha", inner=(record("Inner", one=(int, ...)), ...)),
            record("Alpha", inner=(record("Inner", one=(int, ...), extra=(str, ...)), ...)),
            id="newly required in a nested record",
        ),
        pytest.param(
            record("Alpha", inner=(record("Inner", one=(int, ...)), ...)),
            record("Alpha", one=(int, ...)),
            id="removed definition",
        ),
        pytest.param(
            record("Alpha", mood=(Mood, ...)),
            record("Alpha", mood=(MoodWithPanic, ...)),
            id="added enum value",
        ),
        pytest.param(
            record("Alpha", title=(int, ...)),
            record("Alpha", title=(str, ...)),
            id="a field named title retyped",
        ),
        pytest.param(
            record("Alpha", description=(int, ...)),
            record("Alpha", description=(int, ...), title=(str, ...)),
            id="a field named description beside a new required one",
        ),
        pytest.param(
            record("Alpha", inner=(NAMED_DESCRIPTION, NAMED_DESCRIPTION(description="first"))),
            record("Alpha", inner=(NAMED_DESCRIPTION, NAMED_DESCRIPTION(description="second"))),
            id="a default holding a key named description",
        ),
        pytest.param(
            record("Alpha", by_pattern=(dict[Annotated[str, TITLE_PATTERN], int], ...)),
            record("Alpha", by_pattern=(dict[Annotated[str, TITLE_PATTERN], str], ...)),
            id="a pattern property named title retyped",
        ),
        pytest.param(
            record("Alpha", by_pattern=(dict[Annotated[str, PROPERTIES_PATTERN], int], ...)),
            record(
                "Alpha",
                by_pattern=(
                    dict[Annotated[str, PROPERTIES_PATTERN], Annotated[int, Field(ge=0)]],
                    ...,
                ),
            ),
            id="a pattern property named properties gaining a bound",
        ),
    ],
)
def test_a_resource_schema_difference_other_than_an_added_optional_field_is_named_as_changed(
    before: type[StrictModel], after: type[StrictModel]
) -> None:
    assert line_for(before, after) == CHANGED


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            {"type": "object"}, {"type": "object", "patternProperties": {}}, id="empty map added"
        ),
        pytest.param(
            {"type": "object"}, {"type": "object", "dependentSchemas": {}}, id="empty map of others"
        ),
        pytest.param({"type": "object"}, {"type": "object", "$defs": {}}, id="empty $defs added"),
        pytest.param(
            {"type": "object"}, {"type": "object", "properties": {}}, id="empty properties added"
        ),
        pytest.param(
            {"type": "object", "definitions": {"A": {}}},
            {"type": "object"},
            id="map removed",
        ),
        pytest.param(
            {"type": "object"},
            {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
            id="pattern property added",
        ),
        pytest.param(
            {"type": "object"},
            {"type": "object", "dependentRequired": {"a": ["b"]}},
            id="unknown keyword added",
        ),
        pytest.param(
            {"type": "object", "dependentRequired": {"a": ["b"]}},
            {"type": "object", "dependentRequired": {"a": ["b", "c"]}},
            id="unknown keyword changed",
        ),
        pytest.param(
            {"type": "object", "x-note": {"description": "first"}},
            {"type": "object", "x-note": {"description": "second"}},
            id="prose inside an unknown keyword",
        ),
        pytest.param(
            {"type": "object", "default": {"title": "first"}},
            {"type": "object", "default": {"title": "second"}},
            id="prose inside a default",
        ),
        pytest.param(
            {"type": "object", "enum": [1]},
            {"type": "object", "enum": [True]},
            id="a value equal to another only in Python",
        ),
        pytest.param(
            {"allOf": [{"type": "integer"}]},
            {"allOf": [{"type": "integer"}, {"type": "string"}]},
            id="a list of another length",
        ),
        pytest.param(
            {"$defs": {"A": {"$defs": {"B": {}}}}},
            {"$defs": {"A": {"$defs": {"B": {}, "C": {}}}}},
            id="a $defs entry below the root",
        ),
        pytest.param(
            {"type": "object", "properties": {"one": {"type": "integer"}}},
            {
                "type": "object",
                "properties": {"one": {"type": "integer"}, "two": {"type": "integer"}},
                "required": ["two"],
            },
            id="an added property the new required lists",
        ),
    ],
)
def test_a_schema_difference_the_guard_cannot_read_as_a_gain_is_named_as_changed(
    before: dict[str, object], after: dict[str, object]
) -> None:
    assert diff.note_line(snapshot(BASE, {"Alpha": before}), snapshot(BASE, {"Alpha": after})) == (
        CHANGED
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            {"type": "object", "properties": {"one": {"type": "integer"}}},
            {
                "type": "object",
                "properties": {"one": {"type": "integer"}, "two": {"type": "string"}},
            },
            id="a property",
        ),
        pytest.param(
            {"type": "object"},
            {"type": "object", "properties": {"two": {"type": "string"}}},
            id="a property to a schema with none",
        ),
        pytest.param(
            {"type": "object"},
            {"type": "object", "$defs": {"Mood": {"type": "string"}}},
            id="a $defs entry",
        ),
    ],
)
def test_a_schema_that_only_gains_what_the_old_one_lacks_is_named_as_added(
    before: dict[str, object], after: dict[str, object]
) -> None:
    assert diff.note_line(snapshot(BASE, {"Alpha": before}), snapshot(BASE, {"Alpha": after})) == (
        ADDED
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            record("Alpha", one=(int, ...)), record("Alpha", one=(int, ...)), id="in nothing"
        ),
        pytest.param(
            record("Alpha", "A docstring.", one=(int, Field(description="First"))),
            record("Alpha", "Another docstring.", one=(int, Field(description="Other"))),
            id="only in its description",
        ),
    ],
)
def test_a_resource_record_that_differs_in_nothing_but_prose_is_unchanged(
    before: type[StrictModel], after: type[StrictModel]
) -> None:
    assert line_for(before, after) == "**Member-tool wire:** unchanged."


def test_prose_inside_a_content_schema_leaves_the_record_unchanged() -> None:
    def content_record(description: str) -> dict[str, object]:
        return {
            "Alpha": {
                "type": "string",
                "contentMediaType": "application/json",
                "contentSchema": {"type": "integer", "description": description},
            }
        }

    line = diff.note_line(
        snapshot(BASE, content_record("first")), snapshot(BASE, content_record("second"))
    )

    assert line == "**Member-tool wire:** unchanged."


def test_a_record_added_or_removed_is_named_as_changed() -> None:
    alpha, beta = record("Alpha", one=(int, ...)), record("Beta", two=(int, ...))

    assert diff.note_line(wire_of(Alpha=alpha), wire_of(Alpha=alpha, Beta=beta)) == (
        "**Member-tool wire:** changed: resources: Beta."
    )
    assert diff.note_line(wire_of(Alpha=alpha, Beta=beta), wire_of(Alpha=alpha)) == (
        "**Member-tool wire:** changed: resources: Beta."
    )


def tool_of(arguments: type[StrictModel], *answers: type[StrictModel]) -> dict[str, object]:
    return {
        "arguments": arguments.model_json_schema(),
        "results": [answer.model_json_schema() for answer in answers],
    }


def tool_line(before: dict[str, object], after: dict[str, object]) -> str:
    return diff.note_line(snapshot({"speak": before}), snapshot({"speak": after}))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            tool_of(record("A", "Says.", one=(int, ...))),
            tool_of(record("A", "Says it aloud.", one=(int, ...))),
            id="the arguments' own description",
        ),
        pytest.param(
            tool_of(record("A", one=(int, Field(description="First")))),
            tool_of(record("A", one=(int, Field(description="Other")))),
            id="an argument's description",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...)), record("R", "Heard.", two=(int, ...))),
            tool_of(record("A", one=(int, ...)), record("R", "Was heard.", two=(int, ...))),
            id="an answer's description",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...))),
            tool_of(record("B", one=(int, ...))),
            id="a title",
        ),
    ],
)
def test_a_tool_that_differs_only_in_prose_is_named_as_descriptions_only(
    before: dict[str, object], after: dict[str, object]
) -> None:
    assert tool_line(before, after) == "**Member-tool wire:** descriptions only: speak."


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            tool_of(record("A", one=(int, ...))),
            tool_of(record("A", one=(str, ...))),
            id="a retyped argument",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...))),
            tool_of(record("A", one=(int, ...), extra=(str | None, None))),
            id="an added optional argument",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...))),
            tool_of(record("A", one=(int, ...), extra=(str, ...))),
            id="an added required argument",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...)), record("R", two=(int, ...))),
            tool_of(record("A", one=(int, ...)), record("R", two=(str, ...))),
            id="a retyped answer field",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...)), record("R", two=(int, ...))),
            tool_of(
                record("A", one=(int, ...)),
                record("R", two=(int, ...), extra=(str | None, None)),
            ),
            id="an added optional answer field",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...)), record("R", two=(int, ...))),
            tool_of(record("A", one=(int, ...))),
            id="a removed answer",
        ),
        pytest.param(
            tool_of(record("A", one=(int, ...), mood=(Mood | None, None))),
            tool_of(record("A", one=(int, ...), mood=(MoodWithPanic | None, None))),
            id="an added enum value",
        ),
        pytest.param(
            tool_of(record("A", "Says.", one=(int, ...))),
            tool_of(record("A", "Says it aloud.", one=(str, ...))),
            id="a new description beside a retyped argument",
        ),
    ],
)
def test_a_tool_whose_arguments_or_answers_differ_in_more_than_prose_is_named_as_changed(
    before: dict[str, object], after: dict[str, object]
) -> None:
    assert tool_line(before, after) == "**Member-tool wire:** changed: speak."


def test_a_prose_only_release_with_a_gained_record_names_descriptions_only_then_fields_added() -> (
    None
):
    def tools(prose: str) -> Tools:
        return {
            "speak": tool_of(record("Speak", prose, one=(int, ...))),
            "stop_speaking": tool_of(record("Stop", prose)),
            "gesture": tool_of(record("Gesture", one=(int, ...))),
        }

    before = snapshot(
        tools("Before."), resources_of(PlaceWindow=record("PlaceWindow", a=(int, ...)))
    )
    after = snapshot(
        tools("After."),
        resources_of(PlaceWindow=record("PlaceWindow", a=(int, ...), b=(str | None, None))),
    )

    assert diff.note_line(before, after) == (
        "**Member-tool wire:** descriptions only: speak, stop_speaking. Fields added: PlaceWindow."
    )


def test_the_parts_come_as_changed_then_descriptions_only_then_fields_added() -> None:
    before = {
        "speak": tool_of(record("S", "First.", one=(int, ...))),
        "gesture": tool_of(record("G", one=(int, ...))),
        "stop_speaking": tool_of(record("T", "First.")),
    }
    after = {
        "speak": tool_of(record("S", "Second.", one=(int, ...))),
        "gesture": tool_of(record("G", one=(str, ...))),
        "stop_speaking": tool_of(record("T", "Second.")),
    }
    old_resources = resources_of(
        Alpha=record("Alpha", one=(int, ...)), Beta=record("Beta", a=(int, ...))
    )
    new_resources = resources_of(
        Alpha=record("Alpha", one=(int, ...), extra=(str | None, None)),
        Beta=record("Beta", a=(str, ...)),
    )

    assert diff.note_line(snapshot(before, old_resources), snapshot(after, new_resources)) == (
        "**Member-tool wire:** changed: gesture; resources: Beta."
        " Descriptions only: speak, stop_speaking. Fields added: Alpha."
    )


def test_changes_and_added_fields_share_one_line() -> None:
    before = {"Alpha": record("Alpha", one=(int, ...)), "Beta": record("Beta", two=(int, ...))}
    after = {
        "Alpha": record("Alpha", one=(int, ...), extra=(str | None, None)),
        "Beta": record("Beta", two=(str, ...)),
    }
    changed_tools = {**BASE, "speak": {"arguments": {}, "results": []}}

    assert diff.note_line(wire_of(**before), snapshot(changed_tools, resources_of(**after))) == (
        "**Member-tool wire:** changed: speak; resources: Beta. Fields added: Alpha."
    )


def test_a_previous_snapshot_without_resources_sees_every_record_as_added() -> None:
    records = {"Alpha": record("Alpha", one=(int, ...)), "Beta": record("Beta", two=(int, ...))}

    assert diff.note_line(snapshot(BASE), wire_of(**records)) == (
        "**Member-tool wire:** changed: resources: Alpha, Beta."
    )


def test_without_a_previous_snapshot_the_wire_counts_as_changed() -> None:
    assert diff.note_line(None, snapshot(BASE)) == (
        "**Member-tool wire:** changed (the previous release has no snapshot to compare)."
    )


def test_run_as_a_script_it_prints_exactly_the_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    script = str(Path(diff.__file__).resolve())
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(script, run_name="__main__")

    assert raised.value.code == 0
    assert capsys.readouterr().out == (
        "**Member-tool wire:** changed (the previous release has no snapshot to compare).\n"
    )
