"""Generates ``scripts/member-tool-wire.json``: the member wire a person front declares.

Each tool lists the JSON schema of its arguments record and of every answer record it may return;
``resources`` holds the schema of each record a member resource carries, by the record's name,
all from ``agents.member``. The output is deterministic, so CI regenerates it and requires an empty
diff, and the release note compares it with the previous release's file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from agents.member import MEMBER_RESOURCE_RECORDS, MEMBER_TOOL_ARGUMENTS, MEMBER_TOOL_RESULTS
from agents.tools import tool_parameters

ROOT: Final = Path(__file__).resolve().parent.parent
WIRE_PATH: Final = Path("scripts/member-tool-wire.json")
SHAPE_VERSION: Final = 1


def render() -> str:
    tools = {
        name: {
            "arguments": tool_parameters(arguments),
            "results": [answer.model_json_schema() for answer in MEMBER_TOOL_RESULTS[name]],
        }
        for name, arguments in MEMBER_TOOL_ARGUMENTS.items()
    }
    resources = {record.__name__: record.model_json_schema() for record in MEMBER_RESOURCE_RECORDS}
    document = {"version": SHAPE_VERSION, "tools": tools, "resources": resources}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(*, root: Path) -> int:
    (root / WIRE_PATH).write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(root=ROOT))
