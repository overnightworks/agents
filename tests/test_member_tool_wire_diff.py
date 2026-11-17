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


def snapshot(tools: Tools) -> str:
    return json.dumps({"version": 1, "tools": tools})


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
