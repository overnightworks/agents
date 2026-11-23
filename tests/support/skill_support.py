"""Builders of the skills wire: a listing entry and the SKILL.md text it describes."""

from __future__ import annotations

from pydantic import JsonValue

from agents.skills import skill_digest, skill_uri
from agents.turn import JsonObject

SKILL_DESCRIPTION = "Helps with notes"


def skill_markdown(
    *,
    front_matter: str = f"name: notes-help\ndescription: {SKILL_DESCRIPTION}",
    body: str = "Read.",
) -> bytes:
    return f"---\n{front_matter}\n---\n\n{body}\n".encode()


def a_skill_entry(
    content: bytes = b"",
    *,
    name: str = "notes-help",
    uri: str | None = None,
    frontmatter: JsonObject | None = None,
    digest: str | None = None,
    size: int | None = None,
    **changes: JsonValue,
) -> JsonObject:
    """An entry whose one resource row describes ``content``, each part changeable."""
    skill_file = uri or skill_uri(name)
    row: JsonObject = {
        "uri": skill_file,
        "digest": digest or skill_digest(content),
        "size": len(content) if size is None else size,
    }
    return {
        "uri": skill_file,
        "frontmatter": {"name": name, "description": SKILL_DESCRIPTION} | (frontmatter or {}),
        "resources": [row],
    } | changes
