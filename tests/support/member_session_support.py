"""The arrangement the member session's suites share: an app served behind a scripted gate, a
bearer it lets in, and the records it shows."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import SecretStr

from agents.mcp_client import open_member_session
from agents.member import (
    STANDING_URI,
    MemberStanding,
    PlaceEntry,
    PlaceRef,
    PlaceWindow,
    StandingState,
)
from tests.support.mcp_server_support import MemberApp, a_member, a_standing
from tests.support.scripted_gate import GateReply, ScriptedGate

SUBJECT = "agent-subject"
LIMIT_SECONDS = 10
ROOM = "room-7"


@asynccontextmanager
async def served(app: MemberApp, caplog: pytest.LogCaptureFixture) -> AsyncIterator[ScriptedGate]:
    """The app served behind a gate, and at the end no log line holds a bearer the app was shown."""
    caplog.set_level(0)
    gate = ScriptedGate(app)
    async with gate.serving():
        yield gate
    assert not [shown for shown in gate.presented - {""} if shown in caplog.text]


def a_bearer_let_in(gate: ScriptedGate) -> SecretStr:
    token = SecretStr(secrets.token_urlsafe(24))
    gate.let_in(token.get_secret_value(), SUBJECT)
    return token


def opened(gate: ScriptedGate, bearer: SecretStr, **keywords: Any):
    return open_member_session(gate.url, lambda: bearer, **keywords)


def stands(app: MemberApp, **readable: Any) -> None:
    """The app shows the member the records at these resources, by their place."""
    app.readable[a_member(SUBJECT)] = {
        STANDING_URI: MemberStanding(standings=(a_standing(ROOM, StandingState.ACTIVE),)),
        **readable,
    }


def a_window(*entries: PlaceEntry, **fields: Any) -> PlaceWindow:
    return PlaceWindow(
        generation="generation-1",
        starts_at=entries[0].seq if entries else 1,
        entries=entries,
        **fields,
    )


async def reads_the_standing(gate: ScriptedGate, bearer: SecretStr) -> None:
    async with opened(gate, bearer) as session:
        await session.read_standing()


async def reads_the_window(gate: ScriptedGate, bearer: SecretStr, place: PlaceRef) -> None:
    async with opened(gate, bearer) as session:
        await session.read_place(place)


def answering(status: HTTPStatus) -> Any:
    async def answer(reply: GateReply) -> None:
        await reply.status(status)

    return answer
