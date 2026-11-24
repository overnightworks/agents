"""A runner reads an app's skills through its member session: one page of the listing, one skill."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from mcp_types import (
    DiscoverResult,
    ReadResourceResult,
    ServerCapabilities,
    TextResourceContents,
)
from pydantic import SecretStr

from agents.mcp_wire import PROTOCOL_VERSION
from agents.member_outcomes import INVALID_ANSWER_CODE, UnusableAnswer
from agents.skills import (
    SKILLS_EXTENSION,
    SKILLS_GET_METHOD,
    SKILLS_LIST_METHOD,
    Skill,
    SkillRefusal,
    SkillRefused,
    skill_uri,
)
from agents.turn import JsonObject
from tests.support import member_session_support
from tests.support.member_session_support import opened
from tests.support.scripted_gate import DISCOVER, READ, GateReply, ScriptedGate
from tests.support.skill_support import a_skill_entry, skill_markdown

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
gate = member_session_support.gate
bearer = member_session_support.bearer


def listing(
    entries: list[JsonObject],
    next_cursor: str | None = None,
    asked: list[dict[str, Any]] | None = None,
) -> Any:
    async def lists(reply: GateReply) -> None:
        if asked is not None:
            asked.append(reply.rpc.params)
        await reply.raw_result(
            {"skills": entries} | ({"nextCursor": next_cursor} if next_cursor else {})
        )

    return lists


def serving_file(content: bytes, uri: str) -> Any:
    async def shows(reply: GateReply) -> None:
        contents = TextResourceContents(uri=uri, text=content.decode(), mime_type="text/markdown")
        await reply.result(ReadResourceResult(contents=[contents], ttl_ms=0, cache_scope="private"))

    return shows


@pytest.mark.anyio
async def test_a_listing_is_read_page_by_page_in_the_apps_order(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    entries = [a_skill_entry(name=name) for name in ("one", "two", "three")]
    asked: list[dict[str, Any]] = []
    gate.script(
        SKILLS_LIST_METHOD, listing(entries[:2], "next", asked), listing(entries[2:], asked=asked)
    )

    async with opened(gate, bearer) as session:
        first = await session.list_skills()
        second = await session.list_skills(first.next_cursor)

    assert [entry.name for entry in [*first.skills, *second.skills]] == ["one", "two", "three"]
    assert (first.next_cursor, second.next_cursor) == ("next", None)
    assert [params.get("cursor") for params in asked] == [None, "next"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("pages", "listed", "index"),
    [
        pytest.param([["ok", "broken"]], 0, 1, id="the second entry of the one page"),
        pytest.param(
            [["ok"], ["broken"]],
            1,
            1,
            id="the first entry of the second page counts the page before",
        ),
        pytest.param([[], ["broken"]], 0, 0, id="the first entry after an empty page"),
    ],
)
async def test_a_page_that_breaks_a_rule_fails_naming_the_entry_and_no_text_of_the_app(
    gate: ScriptedGate, bearer: SecretStr, pages: list[list[str]], listed: int, index: int
) -> None:
    marker = secrets.token_hex(8)
    good = a_skill_entry(name="good")
    broken = a_skill_entry(name="good", uri=f"skill://{marker}/not-a-skill-file")
    served = [
        listing([good if each == "ok" else broken for each in page], "next") for page in pages[:-1]
    ]
    served.append(listing([good if each == "ok" else broken for each in pages[-1]]))
    gate.script(SKILLS_LIST_METHOD, *served)

    async with opened(gate, bearer) as session:

        async def lists_every_page() -> None:
            for _ in pages[:-1]:
                await session.list_skills()
            await session.list_skills(listed=listed)

        with pytest.raises(UnusableAnswer) as failure:
            await lists_every_page()

    assert (failure.value.code, failure.value.entry) == (INVALID_ANSWER_CODE, index)
    assert marker not in f"{failure.value} {failure.value!r} {failure.value.answer}"


@pytest.mark.anyio
async def test_a_page_that_is_no_list_of_entries_fails_with_no_index(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    gate.script(SKILLS_LIST_METHOD, lambda reply: reply.raw_result({"skills": "none"}))

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.list_skills()

    assert (failure.value.code, failure.value.entry) == (INVALID_ANSWER_CODE, None)


@pytest.mark.anyio
async def test_keys_a_page_and_its_entries_carry_beyond_the_ones_known_are_ignored(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    entry = a_skill_entry(name="one") | {"added_by_the_app": {"deeper": [1]}}
    gate.script(
        SKILLS_LIST_METHOD,
        lambda reply: reply.raw_result({"skills": [entry], "added_by_the_app": "top"}),
    )

    async with opened(gate, bearer) as session:
        page = await session.list_skills()

    assert [each.name for each in page.skills] == ["one"]


@pytest.mark.anyio
async def test_a_skill_whose_file_matches_its_entry_is_read_under_the_apps_url(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    content = skill_markdown(
        front_matter="name: greeting\ndescription: Helps with notes\nmetadata:\n  tools: say play",
        body="Greet people by name.",
    )
    entry = a_skill_entry(content, name="greeting", frontmatter={"metadata": {"tools": "say play"}})
    asked: list[dict[str, Any]] = []

    async def answers(reply: GateReply) -> None:
        asked.append(reply.rpc.params)
        await reply.raw_result({"skill": entry})

    gate.script(SKILLS_GET_METHOD, answers)
    gate.script(READ, serving_file(content, skill_uri("greeting")))

    async with opened(gate, bearer) as session:
        skill = await session.get_skill(skill_uri("greeting"))

    assert isinstance(skill, Skill)
    assert (skill.name, skill.body, skill.tools) == (
        "greeting",
        "Greet people by name.",
        ("say", "play"),
    )
    assert skill.identity.server == gate.url
    assert gate.count(READ) == 1
    assert [params["uri"] for params in asked] == [skill_uri("greeting")]


@pytest.mark.anyio
async def test_a_skill_whose_file_differs_from_its_entry_is_refused_with_its_code(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    entry = a_skill_entry(skill_markdown(body="Greet people by name."), name="notes-help")
    gate.script(SKILLS_GET_METHOD, lambda reply: reply.raw_result({"skill": entry}))
    gate.script(READ, serving_file(skill_markdown(body="Greet people by none."), str(entry["uri"])))

    async with opened(gate, bearer) as session:
        with pytest.raises(SkillRefused) as refused:
            await session.get_skill(skill_uri("notes-help"))

    assert refused.value.refusal == SkillRefusal.DIGEST_MISMATCH


@pytest.mark.anyio
async def test_an_answer_for_another_skill_than_the_one_asked_for_is_refused_unread(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    other = a_skill_entry(skill_markdown(), name="other")
    gate.script(SKILLS_GET_METHOD, lambda reply: reply.raw_result({"skill": other}))

    async with opened(gate, bearer) as session:
        with pytest.raises(UnusableAnswer) as failure:
            await session.get_skill(skill_uri("greeting"))

    assert failure.value.code == INVALID_ANSWER_CODE
    assert gate.count(READ) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("extensions", "instructions"),
    [
        pytest.param({SKILLS_EXTENSION: {}}, None, id="the extension"),
        pytest.param(None, "What the app says of itself.", id="instructions"),
    ],
)
async def test_the_discovery_says_whether_the_app_declares_skills_and_what_it_says_of_itself(
    gate: ScriptedGate,
    bearer: SecretStr,
    extensions: dict[str, dict[str, Any]] | None,
    instructions: str | None,
) -> None:
    async def discovers(reply: GateReply) -> None:
        capabilities = ServerCapabilities(extensions=extensions)
        await reply.result(
            DiscoverResult(
                supported_versions=[PROTOCOL_VERSION],
                capabilities=capabilities,
                instructions=instructions,
                ttl_ms=0,
            )
        )

    gate.script(DISCOVER, discovers)

    async with opened(gate, bearer) as session:
        discovery = await session.read_discovery()

    assert (discovery.declares_skills, discovery.instructions) == (
        extensions is not None,
        instructions,
    )
    assert gate.count(DISCOVER) == 1
