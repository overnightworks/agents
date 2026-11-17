# ruff: noqa: T201 - the command line's one output owner; it prints the note line
"""Prints the release-note line that says whether the member-tool wire changed.

Takes the current snapshot (``scripts/member-tool-wire.json``) and, optionally, the previous
release's; a tool counts as changed when it was added, removed, or its arguments or results differ.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, cast

LABEL: Final = "**Member-tool wire:**"


def _tools(path: Path) -> dict[str, object]:
    document = cast("dict[str, dict[str, object]]", json.loads(path.read_text(encoding="utf-8")))
    return document["tools"]


def note_line(previous: Path | None, current: Path) -> str:
    if previous is None:
        return f"{LABEL} changed (the previous release has no snapshot to compare)."
    before, after = _tools(previous), _tools(current)
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    if not changed:
        return f"{LABEL} unchanged."
    return f"{LABEL} changed: {', '.join(changed)}."


def main(argv: Sequence[str]) -> int:
    """``previous`` is a snapshot path or ``-`` for none; ``current`` is the snapshot to judge."""
    previous, current = argv
    print(note_line(None if previous == "-" else Path(previous), Path(current)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
