"""The release note line says whether the member-tool wire changed."""

from __future__ import annotations

import io
import json
import runpy
from pathlib import Path

import pytest

import scripts.member_tool_wire_diff as diff

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


RECORDS: dict[str, object] = {
    "Alpha": {"properties": {"one": {}}},
    "Beta": {"properties": {"two": {}}},
}


@pytest.mark.parametrize(
    ("current", "line"),
    [
        (RECORDS, "**Member-tool wire:** unchanged."),
        (
            {**RECORDS, "Alpha": {"properties": {"one": {}, "extra": {}}}},
            "**Member-tool wire:** changed: resources: Alpha.",
        ),
        (
            {**RECORDS, "Alpha": {"properties": {"other": {}}}},
            "**Member-tool wire:** changed: resources: Alpha.",
        ),
        (
            {**RECORDS, "Gamma": {"properties": {}}},
            "**Member-tool wire:** changed: resources: Gamma.",
        ),
        (
            {"Alpha": RECORDS["Alpha"]},
            "**Member-tool wire:** changed: resources: Beta.",
        ),
    ],
)
def test_the_line_names_the_resource_records_that_differ(
    current: dict[str, object], line: str
) -> None:
    assert diff.note_line(snapshot(BASE, RECORDS), snapshot(BASE, current)) == line


def test_the_line_names_tools_and_resource_records_together() -> None:
    current = {**RECORDS, "Alpha": {"properties": {}}}
    changed_tools = {**BASE, "speak": {"arguments": {}, "results": []}}

    assert diff.note_line(snapshot(BASE, RECORDS), snapshot(changed_tools, current)) == (
        "**Member-tool wire:** changed: speak; resources: Alpha."
    )


def test_a_previous_snapshot_without_resources_sees_every_record_as_added() -> None:
    assert diff.note_line(snapshot(BASE), snapshot(BASE, RECORDS)) == (
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
