"""Builders of the skills wire: a listing entry and the SKILL.md text it describes."""

from __future__ import annotations

from pydantic import JsonValue

from agents.skills import skill_digest, skill_uri
from agents.turn import JsonObject

SKILL_DESCRIPTION = "Helps with notes"

_DIGEST_PREFIX = "sha256:"


def _published_digest(shortened: str) -> str:
    return _DIGEST_PREFIX + (shortened * 8)[:64]


# SEP-2640's own skills/list and skills/get example results; each shortened digest is a valid one.
SEP_LIST_RESULT: JsonObject = {
    "resultType": "complete",
    "skills": [
        {
            "uri": "skill://git-workflow/SKILL.md",
            "frontmatter": {
                "name": "git-workflow",
                "description": "Follow this team's Git conventions for branching and commits",
            },
            "resources": [
                {
                    "uri": "skill://git-workflow/SKILL.md",
                    "digest": _published_digest("a1b2c3d4"),
                    "size": 2314,
                }
            ],
        },
        {
            "uri": "skill://acme/billing/refunds/SKILL.md",
            "frontmatter": {
                "name": "refunds",
                "description": "Process customer refund requests per company policy",
                "license": "Apache-2.0",
            },
            "resources": [
                {
                    "uri": "skill://acme/billing/refunds/SKILL.md",
                    "digest": _published_digest("b2c3d4e5"),
                    "size": 3871,
                },
                {
                    "uri": "skill://acme/billing/refunds/examples/email.md",
                    "digest": _published_digest("c3d4e5f6"),
                    "size": 962,
                },
            ],
        },
        {
            "uri": "skill://pdf-processing/SKILL.md",
            "frontmatter": {
                "name": "pdf-processing",
                "description": "Extract, fill, and assemble PDF documents",
                "metadata": {"version": "2.1.0"},
            },
            "resources": [
                {
                    "uri": "skill://pdf-processing/SKILL.md",
                    "digest": _published_digest("d5e6f7a8"),
                    "size": 5120,
                },
                {
                    "uri": "skill://pdf-processing/references/FORMS.md",
                    "digest": _published_digest("e6f7a8b9"),
                    "size": 18433,
                },
                {
                    "uri": "skill://pdf-processing/scripts/extract.py",
                    "digest": _published_digest("f7a8b9c0"),
                    "size": 4096,
                },
                {
                    "uri": "skill://pdf-processing/templates/invoice.md",
                    "digest": _published_digest("a8b9c0d1"),
                    "size": 1210,
                },
                {
                    "uri": "skill://pdf-processing/templates/purchase-order.md",
                    "digest": _published_digest("b9c0d1e2"),
                    "size": 1388,
                },
                {
                    "uri": "skill://pdf-processing/templates/regional/eu-invoice.md",
                    "digest": _published_digest("c0d1e2f3"),
                    "size": 1472,
                },
            ],
        },
    ],
}
SEP_GET_RESULT: JsonObject = {
    "resultType": "complete",
    "skill": SEP_LIST_RESULT["skills"][2],  # type: ignore[index]
}


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
