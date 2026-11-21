"""The release note line says whether the member-tool wire changed."""

from __future__ import annotations

import io
import json
import runpy
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import Field, create_model

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


ADDED = "**Member-tool wire:** fields added: Alpha."
CHANGED = "**Member-tool wire:** changed: resources: Alpha."


def test_a_resource_record_that_differs_in_nothing_is_unchanged() -> None:
    model = record("Alpha", one=(int, ...))

    assert line_for(model, model) == "**Member-tool wire:** unchanged."


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
    ],
)
def test_a_removed_retyped_or_newly_required_field_is_named_as_changed(
    before: type[StrictModel], after: type[StrictModel]
) -> None:
    assert line_for(before, after) == CHANGED


def test_a_resource_record_differing_only_in_its_description_is_unchanged() -> None:
    before = record("Alpha", "A docstring.", one=(int, Field(description="First")))
    after = record("Alpha", "Another docstring.", one=(int, Field(description="Other")))

    assert line_for(before, after) == "**Member-tool wire:** unchanged."


def test_a_record_added_or_removed_is_named_as_changed() -> None:
    alpha, beta = record("Alpha", one=(int, ...)), record("Beta", two=(int, ...))

    assert diff.note_line(wire_of(Alpha=alpha), wire_of(Alpha=alpha, Beta=beta)) == (
        "**Member-tool wire:** changed: resources: Beta."
    )
    assert diff.note_line(wire_of(Alpha=alpha, Beta=beta), wire_of(Alpha=alpha)) == (
        "**Member-tool wire:** changed: resources: Beta."
    )


def test_an_added_optional_argument_still_changes_the_tool() -> None:
    before = {"speak": {"arguments": record("A", one=(int, ...)).model_json_schema()}}
    after = {
        "speak": {
            "arguments": record("A", one=(int, ...), extra=(str | None, None)).model_json_schema()
        }
    }

    assert diff.note_line(snapshot(before), snapshot(after)) == (
        "**Member-tool wire:** changed: speak."
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
