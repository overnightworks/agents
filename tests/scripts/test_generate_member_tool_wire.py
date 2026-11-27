"""The committed member-tool wire snapshot is what the code declares."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

import scripts.generate_member_tool_wire as generator
from agents.mcp_server.resources import MemberResources
from agents.member import MEMBER_RESOURCE_RECORDS, MEMBER_TOOL_ARGUMENTS, MEMBER_TOOL_RESULTS
from tests.support.generator_script import assert_script_writes_its_file_under_the_root


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


def test_the_snapshot_covers_every_record_the_resource_port_can_return() -> None:
    alias, _none = get_args(get_type_hints(MemberResources.read)["return"])

    assert set(MEMBER_RESOURCE_RECORDS) == set(get_args(alias.__value__))


def test_run_as_a_script_it_writes_the_file_under_the_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_script_writes_its_file_under_the_root(
        generator, "generate_member_tool_wire.py", generator.WIRE_PATH, tmp_path, monkeypatch
    )
