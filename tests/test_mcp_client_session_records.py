"""A resource record is read with the keys this release does not know ignored, and refused on a
wrong known field."""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from mcp_types import ReadResourceResult, TextResourceContents
from pydantic import SecretStr

from agents.mcp_client import MemberSession
from agents.member import MemberStanding, PlaceRef, StandingState
from agents.member_outcomes import INVALID_ANSWER_CODE, UnusableAnswer
from tests.support import member_session_support
from tests.support.mcp_server_support import a_standing, a_view
from tests.support.member_session_support import ROOM, opened
from tests.support.member_wire import entry, window_from
from tests.support.scripted_gate import READ, GateReply, ScriptedGate

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
gate = member_session_support.gate
bearer = member_session_support.bearer
place = member_session_support.place


@dataclass(frozen=True)
class ResourceCase:
    """One resource record: the document an app shows, where a known field and a nested record
    sit, and how it is read."""

    document: dict[str, Any]
    known_field: tuple[str, ...]
    nested: tuple[str, ...]
    read: Callable[[MemberSession, PlaceRef], Awaitable[object]]


async def _read_standing(session: MemberSession, _place: PlaceRef) -> object:
    return await session.read_standing()


async def _read_window(session: MemberSession, place: PlaceRef) -> object:
    return await session.read_place(place)


async def _read_view(session: MemberSession, place: PlaceRef) -> object:
    return await session.read_view(place)


@pytest.fixture(params=["standing", "window", "view"])
def resource(request: pytest.FixtureRequest) -> ResourceCase:
    if request.param == "standing":
        standing = MemberStanding(standings=(a_standing(ROOM, StandingState.ACTIVE),))
        document = standing.model_dump(mode="json")
        return ResourceCase(
            document, ("standings", "0", "state"), ("standings", "0"), _read_standing
        )
    if request.param == "window":
        document = window_from(
            {"generation": "g", "starts_at": 1, "entries": [entry(1)]}
        ).model_dump(mode="json")
        return ResourceCase(document, ("entries", "0", "seq"), ("entries", "0"), _read_window)
    document = a_view(3).model_dump(mode="json")
    return ResourceCase(document, ("revision",), (), _read_view)


def _at(document: Any, path: tuple[str, ...]) -> Any:
    for step in path:
        document = document[int(step) if step.isdigit() else step]
    return document


async def reads_the_document(
    gate: ScriptedGate,
    bearer: SecretStr,
    resource: ResourceCase,
    place: PlaceRef,
    document: dict[str, Any],
) -> object:
    async def shows(reply: GateReply) -> None:
        contents = TextResourceContents(
            uri="member://x", text=json.dumps(document), mime_type="application/json"
        )
        await reply.result(ReadResourceResult(contents=[contents], ttl_ms=0, cache_scope="private"))

    gate.script(READ, shows)
    async with opened(gate, bearer) as session:
        return await resource.read(session, place)


async def test_a_record_with_keys_this_release_does_not_know_at_any_depth_is_read_as_before(
    gate: ScriptedGate, bearer: SecretStr, resource: ResourceCase, place: PlaceRef
) -> None:
    expected = await reads_the_document(gate, bearer, resource, place, resource.document)
    served = copy.deepcopy(resource.document)
    _at(served, resource.nested)["added_by_the_app"] = {"deeper": [1]}
    served["added_by_the_app"] = "top"

    assert await reads_the_document(gate, bearer, resource, place, served) == expected


async def test_a_record_with_a_wrong_known_field_is_an_unusable_answer(
    gate: ScriptedGate, bearer: SecretStr, resource: ResourceCase, place: PlaceRef
) -> None:
    served = copy.deepcopy(resource.document)
    _at(served, resource.known_field[:-1])[resource.known_field[-1]] = ["not", "valid"]

    with pytest.raises(UnusableAnswer) as failure:
        await reads_the_document(gate, bearer, resource, place, served)

    assert failure.value.code == INVALID_ANSWER_CODE
