"""The committed member-tool wire snapshot is what the code declares."""

from __future__ import annotations

import json
from pathlib import Path

import scripts.generate_member_tool_wire as generator
from agents.member import MEMBER_RESOURCE_RECORDS, MEMBER_TOOL_ARGUMENTS, MEMBER_TOOL_RESULTS


def test_the_committed_snapshot_is_what_the_code_renders() -> None:
    committed = (generator.ROOT / generator.WIRE_PATH).read_text(encoding="utf-8")

    assert committed == generator.render()


def test_every_member_tool_declares_its_answers() -> None:
    assert MEMBER_TOOL_RESULTS.keys() == MEMBER_TOOL_ARGUMENTS.keys()
    assert all(MEMBER_TOOL_RESULTS.values())


def test_the_snapshot_holds_arguments_and_results_of_each_tool_at_shape_version_one() -> None:
    document = json.loads(generator.render())

    assert document["version"] == 1
    assert document["tools"].keys() == MEMBER_TOOL_ARGUMENTS.keys()
    assert all(
        len(tool["results"]) == len(MEMBER_TOOL_RESULTS[name])
        for name, tool in document["tools"].items()
    )


def test_main_writes_the_snapshot_under_the_root(tmp_path: Path) -> None:
    root = tmp_path
    (root / generator.WIRE_PATH.parent).mkdir(parents=True)

    assert generator.main(root=root) == 0
    assert (root / generator.WIRE_PATH).read_text(encoding="utf-8") == generator.render()


def test_the_snapshot_holds_the_schema_of_each_resource_record_by_name() -> None:
    resources = json.loads(generator.render())["resources"]

    assert resources.keys() == {record.__name__ for record in MEMBER_RESOURCE_RECORDS}
