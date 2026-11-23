"""The YAML front matter of a Markdown file: where it ends, and what it says.

An agent file and a skill file share one format: YAML front matter between two ``---`` lines,
read as YAML 1.2 by ruamel.yaml, then the body as Markdown. Both functions raise ``ValueError``
with a message that does not name the file; the caller prefixes it.
"""

from __future__ import annotations

import io
import re
from typing import Final

from ruamel.yaml import YAML, YAMLError

_FRONT_MATTER_FENCE: Final = "---"
_YAML: Final = YAML(typ="safe", pure=True)
_DUPLICATE_KEY: Final = re.compile(r'found duplicate key "(.*?)" with value')


def split_front_matter(text: str) -> tuple[str, str]:
    """The front matter's YAML text and the body, which has no blank line around it."""
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if not lines or lines[0] != _FRONT_MATTER_FENCE:
        raise ValueError(f"front matter must open with a {_FRONT_MATTER_FENCE} line")
    try:
        closing = lines.index(_FRONT_MATTER_FENCE, 1)
    except ValueError as error:
        raise ValueError(f"front matter must close with a {_FRONT_MATTER_FENCE} line") from error
    return "\n".join(lines[1:closing]), _without_surrounding_blank_lines(lines[closing + 1 :])


def _without_surrounding_blank_lines(lines: list[str]) -> str:
    first = next((index for index, line in enumerate(lines) if line.strip()), len(lines))
    last = next((index for index in reversed(range(len(lines))) if lines[index].strip()), -1)
    return "\n".join(lines[first : last + 1])


def parse_front_matter(text: str) -> dict[str, object]:
    """The front matter's YAML as a mapping, as ruamel's safe loader builds it."""
    try:
        # ruamel's ``load`` leaves its stream parameter untyped, so its type is partly unknown.
        parsed = _YAML.load(io.StringIO(text))  # pyright: ignore[reportUnknownMemberType]
    except YAMLError as error:
        raise ValueError(_yaml_problem(error)) from None
    if not isinstance(parsed, dict):
        raise ValueError("front matter must be a mapping of keys to values")
    return parsed  # pyright: ignore[reportUnknownVariableType]


def _yaml_problem(error: YAMLError) -> str:
    duplicate = _DUPLICATE_KEY.search(str(getattr(error, "problem", None)))
    if duplicate is not None:
        return f"{duplicate[1]}: appears twice"
    mark = getattr(error, "problem_mark", None)
    where = f" at line {mark.line + 1}" if mark is not None else ""
    return f"front matter is not valid YAML{where}"
