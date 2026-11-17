# ruff: noqa: T201 - the command line's one output owner; it prints the note line
"""Prints the release-note line that says whether the member-tool wire changed.

Compares the snapshot beside this script (``member-tool-wire.json``) with the previous release's,
read from standard input (empty when there is none); a tool counts as changed when it was added,
removed, or its arguments or results differ.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final, cast

LABEL: Final = "**Member-tool wire:**"
CURRENT_SNAPSHOT: Final = Path(__file__).resolve().parent / "member-tool-wire.json"


def _tools(snapshot: str) -> dict[str, object]:
    document = cast("dict[str, dict[str, object]]", json.loads(snapshot))
    return document["tools"]


def note_line(previous: str | None, current: str) -> str:
    if previous is None:
        return f"{LABEL} changed (the previous release has no snapshot to compare)."
    before, after = _tools(previous), _tools(current)
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    if not changed:
        return f"{LABEL} unchanged."
    return f"{LABEL} changed: {', '.join(changed)}."


def main(previous: str) -> int:
    """``previous`` is the previous release's snapshot, empty for none."""
    current = CURRENT_SNAPSHOT.read_text(encoding="utf-8")
    print(note_line(previous or None, current))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.read()))
