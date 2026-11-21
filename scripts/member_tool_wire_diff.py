# ruff: noqa: T201 - the command line's one output owner; it prints the note line
"""Prints the release-note line that says whether the member wire changed.

Compares the snapshot beside this script (``member-tool-wire.json``) with the previous release's,
read from standard input (empty when there is none). A tool counts as changed when it was added,
removed, or its arguments or results differ in any byte, descriptions included, which reach the
model. A resource record counts as changed when it was added or removed or its schema differs in
anything but a ``title`` or ``description`` keyword, and as having gained fields when its schema
differs only by properties the previous schema lacks that are not required, and by ``$defs`` entries
it lacks. A snapshot without resources holds none. A schema does not show a ``model_validator``, an
``AfterValidator`` or ``exclude_if``, so a rule across fields is not seen here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final, cast

LABEL: Final = "**Member-tool wire:**"
CURRENT_SNAPSHOT: Final = Path(__file__).resolve().parent / "member-tool-wire.json"

_NAME_MAPS: Final = frozenset({"properties", "$defs"})
_DATA_KEYWORDS: Final = frozenset({"default", "const", "enum", "examples", "discriminator"})
_PROSE_KEYWORDS: Final = frozenset({"title", "description"})

type Schema = dict[str, object]


def _section(snapshot: str, key: str) -> dict[str, object]:
    document = cast("dict[str, dict[str, object]]", json.loads(snapshot))
    return document[key] if key != "resources" else document.get(key, {})


def _without_prose(value: object) -> object:
    """``value`` with every ``title`` and ``description`` keyword removed; names and data stay."""
    if isinstance(value, list):
        return [_without_prose(item) for item in cast("list[object]", value)]
    if not isinstance(value, dict):
        return value
    schema = cast("Schema", value)
    return {
        key: _without_prose_in(key, inner)
        for key, inner in schema.items()
        if key not in _PROSE_KEYWORDS
    }


def _without_prose_in(keyword: str, value: object) -> object:
    if keyword in _DATA_KEYWORDS:
        return value
    if keyword in _NAME_MAPS and isinstance(value, dict):
        named = cast("Schema", value)
        return {name: _without_prose(inner) for name, inner in named.items()}
    return _without_prose(value)


def _reduced(new: object, old: object) -> object:
    """``new`` without the properties and ``$defs`` entries that are additions to ``old``."""
    if isinstance(new, list) and isinstance(old, list):
        items, olds = cast("list[object]", new), cast("list[object]", old)
        if len(items) != len(olds):
            return items
        return [_reduced(item, before) for item, before in zip(items, olds, strict=True)]
    if not (isinstance(new, dict) and isinstance(old, dict)):
        return cast("object", new)
    schema, before = cast("Schema", new), cast("Schema", old)
    required = schema.get("required", [])
    reduced: Schema = {}
    for keyword, value in schema.items():
        if keyword in _DATA_KEYWORDS:
            reduced[keyword] = value
        elif keyword in _NAME_MAPS:
            kept = _kept_names(keyword, cast("Schema", value), before, required)
            if kept or keyword in before:
                reduced[keyword] = kept
        else:
            reduced[keyword] = _reduced(value, before.get(keyword))
    return reduced


def _kept_names(keyword: str, named: Schema, before: Schema, required: object) -> Schema:
    known = cast("Schema", before.get(keyword, {}))
    return {
        name: _reduced(inner, known.get(name))
        for name, inner in named.items()
        if name in known or (keyword == "properties" and name in cast("list[str]", required))
    }


def _classified(previous: str, current: str) -> tuple[list[str], list[str], list[str]]:
    """The tools changed, the resource records changed and the ones that only gained fields."""
    before_tools, after_tools = _section(previous, "tools"), _section(current, "tools")
    tools = sorted(
        name
        for name in before_tools.keys() | after_tools.keys()
        if before_tools.get(name) != after_tools.get(name)
    )
    before, after = _section(previous, "resources"), _section(current, "resources")
    changed: list[str] = []
    gained: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before or name not in after:
            changed.append(name)
            continue
        old, new = _without_prose(before[name]), _without_prose(after[name])
        if old == new:
            continue
        (gained if _reduced(new, old) == old else changed).append(name)
    return tools, changed, gained


def note_line(previous: str | None, current: str) -> str:
    if previous is None:
        return f"{LABEL} changed (the previous release has no snapshot to compare)."
    tools, resources, gained = _classified(previous, current)
    if not tools and not resources and not gained:
        return f"{LABEL} unchanged."
    if not tools and not resources:
        return f"{LABEL} fields added: {', '.join(gained)}."
    parts = [", ".join(tools)] if tools else []
    if resources:
        parts.append(f"resources: {', '.join(resources)}")
    added = f" Fields added: {', '.join(gained)}." if gained else ""
    return f"{LABEL} changed: {'; '.join(parts)}.{added}"


def main(previous: str) -> int:
    """``previous`` is the previous release's snapshot, empty for none."""
    current = CURRENT_SNAPSHOT.read_text(encoding="utf-8")
    print(note_line(previous or None, current))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.read()))
