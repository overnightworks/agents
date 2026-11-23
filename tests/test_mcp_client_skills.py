"""A runner reads an app's skills through an SDK session: one page of the listing, one skill."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client, ClientSession, MCPError
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp_types import (
    DiscoverResult,
    ReadResourceResult,
    RequestParams,
    ServerCapabilities,
    TextResourceContents,
)
from pydantic import SecretStr, ValidationError

from agents.mcp_client.skills import get_skill, list_skills
from agents.mcp_wire import PROTOCOL_VERSION
from agents.member_outcomes import INVALID_ANSWER_CODE, UnusableAnswer
from agents.skills import (
    SKILLS_EXTENSION,
    SKILLS_GET_METHOD,
    SKILLS_LIST_METHOD,
    Skill,
    SkillEntry,
    SkillRefusal,
    SkillRefused,
    skill_uri,
)
from agents.turn import JsonObject
from tests.support import member_session_support
from tests.support.member_session_support import opened
from tests.support.scripted_gate import DISCOVER, READ, GateReply, ScriptedGate
from tests.support.skill_support import (
    SEP_GET_RESULT,
    SEP_LIST_RESULT,
    a_skill_entry,
    skill_markdown,
)

_ENTRY_URI = "skill://pdf-processing/SKILL.md"
_NOT_FOUND = -32002


class _ListParams(RequestParams):
    cursor: str | None = None


class _GetParams(RequestParams):
    uri: str


type _Answer = Callable[[dict[str, Any]], JsonObject]


class StandInApp:
    """An SDK server that answers the two skills methods and keeps what it was asked."""

    def __init__(self, *, on_list: _Answer, on_get: _Answer) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._server: Server[Any] = Server("notes")
        self._answer(SKILLS_LIST_METHOD, _ListParams, on_list)
        self._answer(SKILLS_GET_METHOD, _GetParams, on_get)

    def _answer(self, method: str, params_type: type[RequestParams], answer: _Answer) -> None:
        async def handle(_: ServerRequestContext[Any], params: Any) -> JsonObject:
            asked = params.model_dump(exclude={"meta"}, exclude_none=True)
            self.requests.append((method, asked))
            return answer(asked)

        self._server.add_request_handler(method, params_type, handle)

    @asynccontextmanager
    async def session(self) -> Any:
        async with Client(self._server) as client:
            yield client.session


def an_app(
    *, on_list: _Answer = lambda _: SEP_LIST_RESULT, on_get: _Answer = lambda _: SEP_GET_RESULT
) -> StandInApp:
    return StandInApp(on_list=on_list, on_get=on_get)


def run[T](app: StandInApp, read: Callable[[ClientSession], Awaitable[T]]) -> T:
    async def read_in_session() -> T:
        async with app.session() as session:
            return await read(session)

    return asyncio.run(read_in_session())


def failure_of(app: StandInApp, read: Callable[[ClientSession], Awaitable[object]]) -> Exception:
    """What ``read`` raises; caught inside the session, as leaving it would wrap it in a group."""

    async def read_in_session() -> Exception:
        async with app.session() as session:
            try:
                await read(session)
            except Exception as failure:
                return failure
        raise AssertionError("the read did not fail")

    return asyncio.run(read_in_session())


@pytest.mark.parametrize(
    ("cursor", "sent"), [(None, {}), ("page-2", {"cursor": "page-2"})], ids=["first-page", "cursor"]
)
def test_the_published_listing_arrives_as_a_listing_and_the_request_carries_the_cursor_if_given(
    cursor: str | None, sent: dict[str, str]
) -> None:
    app = an_app()

    listing = run(app, lambda session: list_skills(session, cursor))

    assert [entry.name for entry in listing.skills] == [
        "git-workflow",
        "refunds",
        "pdf-processing",
    ]
    assert app.requests == [(SKILLS_LIST_METHOD, sent)]


def test_following_the_next_cursor_yields_every_entry_once_and_stops_when_it_is_absent() -> None:
    pages = {
        None: {
            "skills": [a_skill_entry(name="one"), a_skill_entry(name="two")],
            "nextCursor": "p2",
        },
        "p2": {"skills": [a_skill_entry(name="three")]},
    }
    app = an_app(on_list=lambda asked: pages[asked.get("cursor")])

    async def read_every_page(session: ClientSession) -> list[SkillEntry]:
        entries: list[SkillEntry] = []
        cursor: str | None = None
        while True:
            page = await list_skills(session, cursor)
            entries.extend(page.skills)
            if page.next_cursor is None:
                return entries
            cursor = page.next_cursor

    entries = run(app, read_every_page)

    assert [entry.name for entry in entries] == ["one", "two", "three"]
    assert [asked for _, asked in app.requests] == [{}, {"cursor": "p2"}]


def test_an_answer_arrives_as_an_answer_and_the_request_carries_the_uri() -> None:
    app = an_app()

    answer = run(app, lambda session: get_skill(session, _ENTRY_URI))

    assert answer.skill.name == "pdf-processing"
    assert app.requests == [(SKILLS_GET_METHOD, {"uri": _ENTRY_URI})]


def test_a_listing_with_one_rule_breaking_entry_fails_as_a_validation_error() -> None:
    breaking = {"skills": [a_skill_entry(name="good-one"), a_skill_entry(name="Bad Name")]}
    app = an_app(on_list=lambda _: breaking)

    assert isinstance(failure_of(app, list_skills), ValidationError)


def test_a_json_rpc_error_answer_leaves_as_the_sdk_raises_it() -> None:
    def refuse(_: dict[str, Any]) -> JsonObject:
        raise MCPError(_NOT_FOUND, "no such skill")

    app = an_app(on_get=refuse)

    refused = failure_of(app, lambda session: get_skill(session, _ENTRY_URI))

    assert isinstance(refused, MCPError)
    assert (refused.error.code, refused.error.message) == (_NOT_FOUND, "no such skill")


# The member session's methods over the same requests, against an app's front on a loopback socket.

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
gate = member_session_support.gate
bearer = member_session_support.bearer


def listing(entries: list[JsonObject], next_cursor: str | None = None) -> Any:
    async def lists(reply: GateReply) -> None:
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
    gate.script(SKILLS_LIST_METHOD, listing(entries[:2], "next"), listing(entries[2:]))

    async with opened(gate, bearer) as session:
        first = await session.list_skills()
        second = await session.list_skills(first.next_cursor)

    assert [entry.name for entry in [*first.skills, *second.skills]] == ["one", "two", "three"]
    assert (first.next_cursor, second.next_cursor) == ("next", None)


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
        with pytest.raises(UnusableAnswer) as failure:
            for _ in pages[:-1]:
                await session.list_skills()
            await session.list_skills(listed=listed)

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
    gate.script(SKILLS_GET_METHOD, lambda reply: reply.raw_result({"skill": entry}))
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
