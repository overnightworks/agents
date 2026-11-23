"""The skills wire: SEP-2640's listing and answer, and what reading one skill proves."""

from __future__ import annotations

import pytest
from pydantic import JsonValue, ValidationError

from agents.skills import (
    SkillAnswer,
    SkillEntry,
    SkillFrontmatter,
    SkillIdentity,
    SkillListing,
    SkillRefusal,
    SkillRefused,
    implicit_skill,
    read_skill,
    skill_uri,
)
from agents.turn import JsonObject
from tests.support.skill_support import SKILL_DESCRIPTION, a_skill_entry, skill_markdown

SERVER = "notes.example.org"

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


def test_the_published_listing_validates_and_keeps_each_entrys_frontmatter() -> None:
    listing = SkillListing.model_validate(SEP_LIST_RESULT)

    assert [entry.name for entry in listing.skills] == ["git-workflow", "refunds", "pdf-processing"]
    assert listing.skills[1].frontmatter["license"] == "Apache-2.0"
    assert listing.skills[2].frontmatter["metadata"] == {"version": "2.1.0"}
    assert [entry.tools for entry in listing.skills] == [(), (), ()]


def test_the_published_answer_validates() -> None:
    answer = SkillAnswer.model_validate(SEP_GET_RESULT)

    assert answer.skill.name == "pdf-processing"


def test_an_entry_at_a_github_uri_validates() -> None:
    entry = a_skill_entry(name="refunds", uri="github://owner/repo/skills/refunds/SKILL.md")

    listing = SkillListing.model_validate({"skills": [entry]})

    assert listing.skills[0].name == "refunds"


def _entry(wire: JsonObject) -> SkillEntry:
    return SkillEntry.model_validate(wire)


def test_a_verified_file_becomes_a_skill_keyed_on_the_label_and_uri_with_its_tools() -> None:
    content = skill_markdown(
        front_matter=(
            "name: notes-help\ndescription: Helps with notes\nlicense: MIT\n"
            "allowed-tools: Bash\nmetadata:\n  tools: lookup save"
        ),
        body="Read first.",
    ).replace(b"\n\nRead", b"\n\n\n\nRead")
    entry = _entry(
        a_skill_entry(
            content,
            frontmatter={
                "license": "MIT",
                "allowed-tools": "Bash",
                "metadata": {"tools": "lookup save"},
            },
        )
    )

    skill = read_skill(SERVER, entry, content)

    assert skill.identity == SkillIdentity(server=SERVER, uri=skill_uri("notes-help"))
    assert (skill.name, skill.description) == ("notes-help", SKILL_DESCRIPTION)
    assert skill.body == "Read first."
    assert skill.tools == ("lookup", "save")
    assert "Bash" not in skill.model_dump_json()


_CONTENT = skill_markdown()


def _refused_case(
    refusal: SkillRefusal,
    content: bytes = _CONTENT,
    *,
    frontmatter: JsonObject | None = None,
    **entry_changes: JsonValue,
) -> tuple[SkillRefusal, bytes, JsonObject]:
    return refusal, content, a_skill_entry(content, frontmatter=frontmatter, **entry_changes)


def _with_front_matter(lines: str) -> bytes:
    return skill_markdown(front_matter=f"name: notes-help\ndescription: Helps with notes\n{lines}")


@pytest.mark.parametrize(
    ("refusal", "content", "wire"),
    [
        pytest.param(*_refused_case(SkillRefusal.DYNAMIC, resources="dynamic"), id="dynamic"),
        pytest.param(
            *_refused_case(SkillRefusal.SIZE_MISMATCH, size=len(_CONTENT) + 1), id="size-longer"
        ),
        pytest.param(
            *_refused_case(SkillRefusal.SIZE_MISMATCH, size=len(_CONTENT) - 1), id="size-shorter"
        ),
        pytest.param(
            SkillRefusal.DIGEST_MISMATCH,
            _CONTENT.replace(b"Read", b"Rend"),
            a_skill_entry(_CONTENT),
            id="one-changed-byte",
        ),
        pytest.param(
            *_refused_case(SkillRefusal.FRONTMATTER_MISMATCH, frontmatter={"description": "Other"}),
            id="entry-frontmatter-differs",
        ),
        pytest.param(
            *_refused_case(
                SkillRefusal.FRONTMATTER_MISMATCH,
                _with_front_matter("x: true"),
                frontmatter={"x": 1},
            ),
            id="yaml-bool-json-integer",
        ),
        pytest.param(
            *_refused_case(
                SkillRefusal.FRONTMATTER_MISMATCH,
                _with_front_matter("x: 1"),
                frontmatter={"x": 1.0},
            ),
            id="yaml-integer-json-float",
        ),
        pytest.param(
            *_refused_case(
                SkillRefusal.FRONTMATTER_MISMATCH,
                _with_front_matter("x: 2024-01-01"),
                frontmatter={"x": "2024-01-01"},
            ),
            id="yaml-date-json-string",
        ),
        pytest.param(
            *_refused_case(
                SkillRefusal.FRONTMATTER_MISMATCH,
                _with_front_matter("a: &shared {k: v}\nb: *shared"),
                frontmatter={"a": {"k": "v"}, "b": {"k": "v"}},
            ),
            id="aliased-mapping",
        ),
        pytest.param(
            *_refused_case(SkillRefusal.FRONTMATTER_MISMATCH, b"---\nname: \xff\n---\n"),
            id="not-utf-8",
        ),
        pytest.param(
            *_refused_case(SkillRefusal.FRONTMATTER_MISMATCH, b"Just text.\n"),
            id="no-front-matter",
        ),
        pytest.param(
            *_refused_case(SkillRefusal.FRONTMATTER_MISMATCH, _with_front_matter("x: [")),
            id="invalid-yaml",
        ),
    ],
)
def test_a_file_that_is_not_what_its_entry_promised_is_refused(
    refusal: SkillRefusal, content: bytes, wire: JsonObject
) -> None:
    with pytest.raises(SkillRefused) as refused:
        read_skill(SERVER, _entry(wire), content)

    assert refused.value.refusal is refusal


def test_an_entry_validates_its_front_matter_once_however_often_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations: list[object] = []
    validate = SkillFrontmatter.model_validate.__func__  # type: ignore[attr-defined]

    def counting(cls: type[SkillFrontmatter], wire: object) -> SkillFrontmatter:
        validations.append(wire)
        return validate(cls, wire)  # type: ignore[no-any-return]

    monkeypatch.setattr(SkillFrontmatter, "model_validate", classmethod(counting))

    entry = _entry(a_skill_entry(_CONTENT))
    read_skill(SERVER, entry, _CONTENT)

    assert len(validations) == 1


def test_unknown_keys_at_every_level_are_ignored_and_the_next_cursor_is_kept() -> None:
    entry = a_skill_entry(b"x", _meta={"k": "v"})
    entry["resources"][0]["extra"] = 1  # type: ignore[index]
    result = {
        "skills": [entry],
        "nextCursor": "c2",
        "ttlMs": 5,
        "cacheScope": "shared",
        "_meta": {},
    }

    listing = SkillListing.model_validate(result)

    assert listing.next_cursor == "c2"
    assert listing.skills[0].name == "notes-help"


def _without_resources() -> JsonObject:
    entry = a_skill_entry()
    del entry["resources"]
    return entry


_OTHER_ROW: JsonValue = [
    {"uri": "skill://notes-help/other.md", "digest": "sha256:" + "a" * 64, "size": 1}
]


@pytest.mark.parametrize(
    "bad_entry",
    [
        pytest.param(a_skill_entry(name="Bad"), id="name-uppercase"),
        pytest.param(a_skill_entry(name="a--b"), id="name-double-hyphen"),
        pytest.param(a_skill_entry(name="-a"), id="name-leading-hyphen"),
        pytest.param(a_skill_entry(name="\u00fc"), id="name-not-ascii"),
        pytest.param(a_skill_entry(name="a" * 65), id="name-65-characters"),
        pytest.param(
            a_skill_entry(uri="skill://other/SKILL.md"), id="name-not-the-segment-before-the-file"
        ),
        pytest.param(
            a_skill_entry(uri="skill://notes-help/README.md"), id="uri-not-the-skill-file"
        ),
        pytest.param(a_skill_entry(frontmatter={"description": ""}), id="description-empty"),
        pytest.param(a_skill_entry(frontmatter={"description": "d" * 1025}), id="description-1025"),
        pytest.param(
            a_skill_entry(frontmatter={"compatibility": "c" * 501}), id="compatibility-501"
        ),
        pytest.param(a_skill_entry(frontmatter={"compatibility": None}), id="compatibility-null"),
        pytest.param(a_skill_entry(frontmatter={"metadata": None}), id="metadata-null"),
        pytest.param(a_skill_entry(frontmatter={"metadata": {"k": 1}}), id="metadata-not-a-string"),
        pytest.param(a_skill_entry(frontmatter={"metadata": {"tools": ""}}), id="tools-empty"),
        pytest.param(a_skill_entry(frontmatter={"metadata": {"tools": "a  b"}}), id="tools-gap"),
        pytest.param(
            a_skill_entry(frontmatter={"metadata": {"tools": " a"}}), id="tools-leading-space"
        ),
        pytest.param(
            a_skill_entry(frontmatter={"metadata": {"tools": "a a"}}), id="tools-repeated"
        ),
        pytest.param(a_skill_entry(digest="sha256:" + "A" * 64), id="digest-uppercase"),
        pytest.param(a_skill_entry(digest="sha256:abc"), id="digest-short"),
        pytest.param(a_skill_entry(size=-1), id="size-negative"),
        pytest.param(_without_resources(), id="resources-absent"),
        pytest.param(a_skill_entry(resources="other"), id="resources-other-string"),
        pytest.param(a_skill_entry(resources=_OTHER_ROW), id="no-row-for-the-skill-file"),
    ],
)
def test_an_entry_breaking_a_rule_fails_the_whole_listing_naming_the_entry(
    bad_entry: JsonObject,
) -> None:
    listing = {
        "skills": [a_skill_entry(name="good-one"), bad_entry, a_skill_entry(name="good-two")]
    }

    with pytest.raises(ValidationError) as failed:
        SkillListing.model_validate(listing)

    assert {error["loc"][:2] for error in failed.value.errors()} == {("skills", 1)}


@pytest.mark.parametrize(
    ("url", "instructions", "name"),
    [
        pytest.param(
            "https://Notes.Example.org:8443/mcp", "Be brief.", "notes-example-org", id="host"
        ),
        pytest.param(
            "https://notes.example.org/mcp", None, "notes-example-org", id="no-instructions"
        ),
        pytest.param("http://127.0.0.1:9000/mcp", "x", "127-0-0-1", id="address"),
        pytest.param(
            "https://xn--bcher-kva.example/mcp", "x", "xn-bcher-kva-example", id="punycode"
        ),
    ],
)
def test_the_implicit_skill_is_named_from_the_urls_host(
    url: str, instructions: str | None, name: str
) -> None:
    skill = implicit_skill(SERVER, url, instructions, ["lookup", "save"], SKILL_DESCRIPTION)

    assert skill.name == name
    assert skill.identity == SkillIdentity(server=SERVER, uri=f"skill://{name}/SKILL.md")
    assert skill.body == (instructions or "")
    assert skill.tools == ("lookup", "save")


@pytest.mark.parametrize(
    ("host", "length"),
    [
        pytest.param(".".join(["a" * 20] * 12), 64, id="cut-inside-a-label"),
        pytest.param(".".join(["a" * 63] * 2), 63, id="cut-on-a-hyphen"),
    ],
)
def test_a_long_host_is_cut_to_a_valid_name_without_a_trailing_hyphen(
    host: str, length: int
) -> None:
    skill = implicit_skill(SERVER, f"https://{host}/mcp", None, [], SKILL_DESCRIPTION)

    assert len(skill.name) == length
    assert not skill.name.endswith("-")
    SkillEntry.model_validate(a_skill_entry(name=skill.name))


def test_hyphens_at_either_end_of_the_host_are_stripped() -> None:
    skill = implicit_skill(SERVER, "https://-.notes.example.org./mcp", None, [], SKILL_DESCRIPTION)

    assert skill.name == "notes-example-org"


@pytest.mark.parametrize(
    ("server", "url", "tools", "description"),
    [
        pytest.param(SERVER, "secret-token", [], SKILL_DESCRIPTION, id="url-without-host"),
        pytest.param(
            SERVER, "https://secret-token@[::1/", [], SKILL_DESCRIPTION, id="malformed-host"
        ),
        pytest.param(SERVER, "https://.../mcp", [], SKILL_DESCRIPTION, id="host-without-a-z0-9"),
        pytest.param(
            SERVER, "https://notes.example.org", ["a", "a"], SKILL_DESCRIPTION, id="repeated-tool"
        ),
        pytest.param(SERVER, "https://notes.example.org", [], "", id="empty-description"),
        pytest.param("", "https://notes.example.org", [], SKILL_DESCRIPTION, id="empty-label"),
    ],
)
def test_an_implicit_skill_without_a_host_a_label_or_a_description_is_refused_without_the_url(
    server: str, url: str, tools: list[str], description: str
) -> None:
    with pytest.raises(ValueError, match=r"^(?!.*secret-token)") as refused:
        implicit_skill(server, url, None, tools, description)

    assert "secret-token" not in str(refused.value)
