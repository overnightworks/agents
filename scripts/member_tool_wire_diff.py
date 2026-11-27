# ruff: noqa: T201 - the command line's one output owner; it prints the note line
"""Prints the release-note line that says whether the member wire changed.

Compares the snapshot beside this script (``member-tool-wire.json``) with the previous release's,
read from standard input (empty when there is none). A tool counts as changed when it was added,
removed, or its arguments or results differ in anything but a ``title`` or ``description``
keyword of a schema the guard walks, a gained optional property included; a tool that differs only
in such prose is named apart, since descriptions reach the model. A resource record counts as
changed when it was added or removed or its schema differs in the same way, and as having gained
fields when its schema differs only by properties the previous schema lacks that are not required,
and by ``$defs`` entries it lacks. A keyword the guard does not know is compared byte for byte, so
it can only make a tool or record changed. A snapshot without resources holds none. A schema does
not show a ``model_validator``, an ``AfterValidator`` or ``exclude_if``, so a rule across fields is
not seen here.
"""

from __future__ import annotations

import json
import sys
from enum import IntEnum
from pathlib import Path
from typing import Final, cast

LABEL: Final = "**Member-tool wire:**"
CURRENT_SNAPSHOT: Final = Path(__file__).resolve().parent / "member-tool-wire.json"

_PROPERTIES: Final = "properties"
_DEFINITIONS: Final = "$defs"

# keywords whose value maps names or patterns to schemas, and those holding one schema or a list
_SCHEMA_MAPS: Final = frozenset(
    {_PROPERTIES, _DEFINITIONS, "definitions", "patternProperties", "dependentSchemas"}
)
_SUBSCHEMAS: Final = frozenset(
    {
        "items",
        "prefixItems",
        "additionalItems",
        "contains",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "propertyNames",
        "contentSchema",
        "not",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
    }
)
_PROSE_KEYWORDS: Final = frozenset({"title", "description"})

type Schema = dict[str, object]


class _Difference(IntEnum):
    NONE = 0
    GAINED = 1
    CHANGED = 2


def _section(snapshot: str, key: str) -> dict[str, object]:
    document = cast("dict[str, dict[str, object]]", json.loads(snapshot))
    return document[key] if key != "resources" else document.get(key, {})


def _compared_bytes(old: object, new: object) -> _Difference:
    same = json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True)
    return _Difference.NONE if same else _Difference.CHANGED


def _map_of(value: object) -> Schema | None:
    return cast("Schema", value) if isinstance(value, dict) else None


def _list_of(value: object) -> list[object] | None:
    return cast("list[object]", value) if isinstance(value, list) else None


def _compared_schemas(old: object, new: object, *, root: bool = False) -> _Difference:
    """How schema ``new`` differs from ``old``; only a keyword known to hold schemas is walked.

    Exactly two differences gain: a property the old schema lacks and ``required`` does not list,
    and a ``$defs`` entry of the root the old schema lacks. Any other difference, an added or
    removed keyword included, is a change, so a keyword not listed above can only make one.
    """
    before, after = _map_of(old), _map_of(new)
    if before is None or after is None:
        return _compared_bytes(old, new)
    required = _list_of(after.get("required"))
    listed = cast("list[str]", required) if required is not None else []
    result = _Difference.NONE
    for keyword in (before.keys() | after.keys()) - _PROSE_KEYWORDS:
        if keyword in before and keyword in after:
            difference = _compared_keyword(keyword, before[keyword], after[keyword], listed, root)
        elif _is_gaining_map(keyword, root) and (added := _map_of(after.get(keyword))):
            difference = _compared_names(keyword, {}, added, listed, root)
        else:
            difference = _Difference.CHANGED
        result = max(result, difference)
    return result


def _is_gaining_map(keyword: str, root: bool) -> bool:
    return keyword == _PROPERTIES or (keyword == _DEFINITIONS and root)


def _is_gain(keyword: str, name: str, required: list[str], root: bool) -> bool:
    return (keyword == _PROPERTIES and name not in required) or (keyword == _DEFINITIONS and root)


def _compared_keyword(
    keyword: str, old: object, new: object, required: list[str], root: bool
) -> _Difference:
    old_map, new_map = _map_of(old), _map_of(new)
    if keyword in _SCHEMA_MAPS and old_map is not None and new_map is not None:
        return _compared_names(keyword, old_map, new_map, required, root)
    if keyword in _SUBSCHEMAS:
        old_list, new_list = _list_of(old), _list_of(new)
        if old_list is not None and new_list is not None:
            return _compared_each(old_list, new_list)
        return _compared_schemas(old, new)
    return _compared_bytes(old, new)


def _compared_each(old: list[object], new: list[object]) -> _Difference:
    if len(old) != len(new):
        return _Difference.CHANGED
    pairs = zip(old, new, strict=True)
    return max((_compared_schemas(o, n) for o, n in pairs), default=_Difference.NONE)


def _compared_names(
    keyword: str, old: Schema, new: Schema, required: list[str], root: bool
) -> _Difference:
    result = _Difference.NONE
    for name in old.keys() | new.keys():
        if name in old and name in new:
            difference = _compared_schemas(old[name], new[name])
        elif name in new and _is_gain(keyword, name, required, root):
            difference = _Difference.GAINED
        else:
            difference = _Difference.CHANGED
        result = max(result, difference)
    return result


def _compared_tools(old: Schema, new: Schema) -> _Difference:
    """How the arguments and the answers of tool ``new`` differ from those of ``old``.

    The result is ``NONE`` when only prose differs; a gain counts as a difference here, since
    arguments and answers are strict.
    """
    result = _Difference.NONE
    for key in old.keys() | new.keys():
        old_part, new_part = old.get(key), new.get(key)
        old_list, new_list = _list_of(old_part), _list_of(new_part)
        if old_list is not None and new_list is not None:
            difference = _compared_each(old_list, new_list)
        else:
            difference = _compared_schemas(old_part, new_part)
        result = max(result, difference)
    return result


def _classified_tools(previous: str, current: str) -> tuple[list[str], list[str]]:
    """The tools whose shape changed and the ones that differ only in prose."""
    before, after = _section(previous, "tools"), _section(current, "tools")
    changed: list[str] = []
    descriptions_only: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        old, new = _map_of(before.get(name)), _map_of(after.get(name))
        if old is None or new is None:
            changed.append(name)
        elif _compared_bytes(old, new) is _Difference.CHANGED:
            shape_kept = _compared_tools(old, new) is _Difference.NONE
            (descriptions_only if shape_kept else changed).append(name)
    return changed, descriptions_only


def _classified_resources(previous: str, current: str) -> tuple[list[str], list[str]]:
    """The resource records changed and the ones that only gained fields."""
    before, after = _section(previous, "resources"), _section(current, "resources")
    changed: list[str] = []
    gained: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before or name not in after:
            changed.append(name)
            continue
        difference = _compared_schemas(before[name], after[name], root=True)
        if difference is _Difference.GAINED:
            gained.append(name)
        elif difference is _Difference.CHANGED:
            changed.append(name)
    return changed, gained


def note_line(previous: str | None, current: str) -> str:
    if previous is None:
        return f"{LABEL} changed (the previous release has no snapshot to compare)."
    tools, descriptions_only = _classified_tools(previous, current)
    resources, gained = _classified_resources(previous, current)
    changed = [", ".join(tools)] if tools else []
    if resources:
        changed.append(f"resources: {', '.join(resources)}")
    parts = [
        f"{label}: {names}."
        for label, names in (
            ("changed", "; ".join(changed)),
            ("descriptions only", ", ".join(descriptions_only)),
            ("fields added", ", ".join(gained)),
        )
        if names
    ]
    if not parts:
        return f"{LABEL} unchanged."
    return f"{LABEL} {parts[0]}" + "".join(f" {part[0].upper()}{part[1:]}" for part in parts[1:])


def main(previous: str) -> int:
    """``previous`` is the previous release's snapshot, empty for none."""
    current = CURRENT_SNAPSHOT.read_text(encoding="utf-8")
    print(note_line(previous or None, current))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.read()))
