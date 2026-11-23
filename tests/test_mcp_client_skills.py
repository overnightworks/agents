"""A runner reads an app's skills through an SDK session: one page of the listing, one skill."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client, ClientSession, MCPError
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp_types import RequestParams
from pydantic import ValidationError

from agents.mcp_client.skills import get_skill, list_skills
from agents.skills import SKILLS_GET_METHOD, SKILLS_LIST_METHOD, SkillEntry
from agents.turn import JsonObject
from tests.support.skill_support import SEP_GET_RESULT, SEP_LIST_RESULT, a_skill_entry

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
