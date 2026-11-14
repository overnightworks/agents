"""Generates ``src/agents/agents-vocabulary.json`` from ``agents.turn.StopOutcome``.

The wheel carries the file so a kit that shows outcomes reads the closed set from the release it
pins instead of copying it. ``version`` is the shape version of the file. The output is
deterministic, so CI regenerates it and requires an empty diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from agents.turn import StopOutcome

ROOT: Final = Path(__file__).resolve().parent.parent
VOCABULARY_PATH: Final = Path("src/agents/agents-vocabulary.json")
SHAPE_VERSION: Final = 1


def render() -> str:
    document = {
        "version": SHAPE_VERSION,
        "stop_outcomes": [outcome.value for outcome in StopOutcome],
    }
    return json.dumps(document, indent=2) + "\n"


def main(*, root: Path) -> int:
    (root / VOCABULARY_PATH).write_text(render(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(root=ROOT))
