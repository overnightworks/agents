"""An app's skills as SEP-2640 (the MCP skills extension) serves them.

``SkillsListParams`` and ``SkillsGetParams`` are the params of ``skills/list`` and ``skills/get``,
which a runner sends and an app's front parses; ``SkillListing`` and ``SkillAnswer`` read their
results;
an entry that breaks the Agent Skills rules for its name, description, compatibility or metadata
fails the listing whole. ``read_skill`` verifies a ``SKILL.md`` against its entry and returns the
``Skill`` a runner offers its model, keyed on the host's label for the server and the entry's uri,
never on the name alone. ``implicit_skill`` names the one skill of an app that declares no
skills extension, from the host of the app's URL.

The Agent Skills name rule is narrowed to ASCII ``a-z`` and ``0-9``, where the specification says
"unicode lowercase alphanumeric". ``allowed-tools`` is never read: a skill widens no grant.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from enum import StrEnum
from functools import cached_property
from typing import Annotated, Final, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from agents.front_matter import parse_front_matter, split_front_matter
from agents.turn import JsonObject

SKILLS_EXTENSION: Final = "io.modelcontextprotocol/skills"
SKILLS_LIST_METHOD: Final = "skills/list"
SKILLS_GET_METHOD: Final = "skills/get"
SKILL_FILE_NAME: Final = "SKILL.md"
TOOLS_METADATA_KEY: Final = "tools"
"""The ``metadata`` key a skill lists the tools it uses under, separated by single spaces."""

_SKILL_SCHEME: Final = "skill"
_DIGEST_PREFIX: Final = "sha256:"
_MAX_NAME_LENGTH: Final = 64
_NON_NAME_RUN: Final = re.compile(r"[^a-z0-9]+")
_TOOL_NAME: Final = re.compile(r"\S+")
_NO_HOST: Final = "the server URL has no host to name its implicit skill"

SkillName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=_MAX_NAME_LENGTH, strict=True
    ),
]
"""The Agent Skills name: 1 to 64 of ``a-z``, ``0-9`` and single hyphens, none at either end."""

SkillDescription = Annotated[StrictStr, StringConstraints(min_length=1, max_length=1024)]
_Compatibility = Annotated[StrictStr, StringConstraints(min_length=1, max_length=500)]
_Digest = Annotated[StrictStr, StringConstraints(pattern=rf"^{_DIGEST_PREFIX}[0-9a-f]{{64}}$")]


def skill_uri(name: SkillName) -> str:
    """The uri of a skill under the ``skill://`` scheme, whose authority is its name."""
    return f"{_SKILL_SCHEME}://{name}/{SKILL_FILE_NAME}"


def skill_digest(content: bytes) -> str:
    """The digest a listing carries for a file's raw bytes: ``sha256:`` and 64 lowercase hex."""
    return f"{_DIGEST_PREFIX}{hashlib.sha256(content).hexdigest()}"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class SkillResource(_Record):
    """One file of a skill: its uri, digest and length in bytes."""

    uri: StrictStr
    digest: _Digest
    size: StrictInt = Field(ge=0)


class SkillFrontmatter(_Record):
    """The Agent Skills fields a runner reads; the rest of a skill's front matter passes by."""

    name: SkillName
    description: SkillDescription
    compatibility: _Compatibility | None = None
    metadata: dict[StrictStr, StrictStr] | None = None

    @property
    def tools(self) -> tuple[str, ...]:
        listed = (self.metadata or {}).get(TOOLS_METADATA_KEY)
        return () if listed is None else tuple(listed.split(" "))

    @model_validator(mode="after")
    def _refuse_malformed_optional_fields(self) -> Self:
        for field in ("compatibility", "metadata"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must be left out, not null")
        tools = self.tools
        if not all(_TOOL_NAME.fullmatch(tool) for tool in tools):
            raise ValueError("metadata.tools must be tool names separated by single spaces")
        if len(set(tools)) != len(tools):
            raise ValueError("metadata.tools names a tool twice")
        return self


class SkillEntry(_Record):
    """A ``skills/list`` or ``skills/get`` entry; ``frontmatter`` is kept as the app sent it."""

    uri: StrictStr
    frontmatter: JsonObject
    resources: tuple[SkillResource, ...] | Literal["dynamic"]

    @model_validator(mode="after")
    def _refuse_a_malformed_entry(self) -> Self:
        file_suffix = f"/{SKILL_FILE_NAME}"
        if not self.uri.endswith(file_suffix):
            raise ValueError(f"uri must end in {file_suffix}")
        if self.uri.removesuffix(file_suffix).rsplit("/", 1)[-1] != self.name:
            raise ValueError("the uri segment before the file name must equal the skill name")
        if (
            not isinstance(self.resources, str)
            and [row.uri for row in self.resources].count(self.uri) != 1
        ):
            raise ValueError("resources must list the skill file exactly once")
        return self

    @property
    def name(self) -> str:
        return self._frontmatter_view.name

    @property
    def description(self) -> str:
        return self._frontmatter_view.description

    @property
    def tools(self) -> tuple[str, ...]:
        return self._frontmatter_view.tools

    @cached_property
    def _frontmatter_view(self) -> SkillFrontmatter:
        return SkillFrontmatter.model_validate(self.frontmatter)


class SkillsListParams(_Record):
    """The params of ``skills/list``: the cursor of the page to read, absent for the first."""

    cursor: StrictStr | None = None


class SkillsGetParams(_Record):
    """The params of ``skills/get``: the uri of the skill's ``SKILL.md``."""

    uri: StrictStr


class SkillListing(_Record):
    """The result of ``skills/list``: a page of entries and the cursor of the next one."""

    skills: tuple[SkillEntry, ...]
    next_cursor: StrictStr | None = Field(default=None, alias="nextCursor")


class SkillAnswer(_Record):
    """The result of ``skills/get``."""

    skill: SkillEntry


class SkillIdentity(_Record):
    """What identifies a skill: the host's label for its server and the uri of its file."""

    server: Annotated[str, StringConstraints(min_length=1)]
    uri: str


class Skill(_Record):
    """A skill a runner offers its model, verified against its entry."""

    identity: SkillIdentity
    name: SkillName
    description: SkillDescription
    body: str
    tools: tuple[str, ...]


class SkillRefusal(StrEnum):
    DYNAMIC = "dynamic"
    SIZE_MISMATCH = "size_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    FRONTMATTER_MISMATCH = "frontmatter_mismatch"


class SkillRefused(Exception):
    """A skill is not loaded: its entry is dynamic, or its file is not what the entry promised."""

    def __init__(self, refusal: SkillRefusal) -> None:
        super().__init__(f"the skill is refused: {refusal.value}")
        self.refusal = refusal


def read_skill(server: str, entry: SkillEntry, content: bytes) -> Skill:
    """The skill an entry describes, once ``content`` proves to be its ``SKILL.md``.

    Raises ``SkillRefused`` for a dynamic entry (the SEP lets a host decline it), a length or
    digest that differs from the entry's row, or front matter that differs from the entry's
    ``frontmatter`` in value, in JSON type or by a node an alias shares.
    """
    if isinstance(entry.resources, str):
        raise SkillRefused(SkillRefusal.DYNAMIC)
    row = next(row for row in entry.resources if row.uri == entry.uri)
    if len(content) != row.size:
        raise SkillRefused(SkillRefusal.SIZE_MISMATCH)
    if skill_digest(content) != row.digest:
        raise SkillRefused(SkillRefusal.DIGEST_MISMATCH)
    return Skill(
        identity=SkillIdentity(server=server, uri=entry.uri),
        name=entry.name,
        description=entry.description,
        body=_body_of_matching_file(entry, content),
        tools=entry.tools,
    )


def _body_of_matching_file(entry: SkillEntry, content: bytes) -> str:
    try:
        front_matter, body = split_front_matter(content.decode("utf-8"))
        parsed = parse_front_matter(front_matter)
    except ValueError:
        raise SkillRefused(SkillRefusal.FRONTMATTER_MISMATCH) from None
    if not _same_json(parsed, entry.frontmatter, set()):
        raise SkillRefused(SkillRefusal.FRONTMATTER_MISMATCH)
    return body


def _same_json(parsed: object, verbatim: object, containers: set[int]) -> bool:
    """Whether YAML ``parsed`` is ``verbatim``'s JSON in value and type, sharing no container."""
    if type(parsed) is not type(verbatim):
        return False
    if isinstance(parsed, dict):
        mapping = cast("dict[object, object]", parsed)
        expected = cast("dict[object, object]", verbatim)
        return (
            _first_visit(mapping, containers)
            and mapping.keys() == expected.keys()
            and all(_same_json(value, expected[key], containers) for key, value in mapping.items())
        )
    if isinstance(parsed, list):
        items = cast("list[object]", parsed)
        expected_items = cast("list[object]", verbatim)
        return (
            _first_visit(items, containers)
            and len(items) == len(expected_items)
            and all(
                _same_json(item, expected_item, containers)
                for item, expected_item in zip(items, expected_items, strict=True)
            )
        )
    return parsed == verbatim


def _first_visit(container: object, containers: set[int]) -> bool:
    if id(container) in containers:
        return False
    containers.add(id(container))
    return True


def implicit_skill(
    server: str,
    url: str,
    instructions: str | None,
    tools: Sequence[str],
    description: str,
) -> Skill:
    """The one skill of an app that declares no skills extension, named from its URL's host.

    The name is the host in lowercase with each run of characters outside ``a-z0-9`` made one
    hyphen, hyphens stripped at both ends, cut to 64 characters and stripped again; its uri is
    never read, the app serves no such resource. The body is the app's ``instructions``; the tools
    are the app's own, in the order it lists them.
    Raises ``ValueError`` for a URL without a host, a repeated tool, or an empty label or
    description; no message echoes the URL, which may carry userinfo.
    """
    name = _name_from_host(url)
    if len(set(tools)) != len(tools):
        raise ValueError("a tool is listed twice")
    return Skill(
        identity=SkillIdentity(server=server, uri=skill_uri(name)),
        name=name,
        description=description,
        body=instructions or "",
        tools=tuple(tools),
    )


def _name_from_host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    name = _NON_NAME_RUN.sub("-", host).strip("-")[:_MAX_NAME_LENGTH].rstrip("-")
    if not name:
        raise ValueError(_NO_HOST)
    return name
