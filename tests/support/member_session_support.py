"""The arrangement the member session's suites share: an app served behind a scripted gate, a
bearer it lets in, and the records it shows."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import pytest
from pydantic import SecretStr

from agents.mcp_client import open_member_session
from agents.mcp_server import (
    InMemoryPlaceStore,
    PersonRequest,
    PersonTool,
    ResourceChanges,
)
from agents.member import (
    MEMBER_TOOL_ARGUMENTS,
    SPEAK_TOOL,
    STANDING_URI,
    MemberStanding,
    PlaceEntry,
    PlaceRef,
    PlaceWindow,
    Spoken,
    StandingState,
)
from agents.tools import JsonObject, ToolDeclaration, ToolEffect, ToolOutcome, tool_parameters
from tests.support.mcp_server_support import MemberApp, a_member, a_standing
from tests.support.scripted_gate import GateReply, ScriptedGate

SUBJECT = "agent-subject"
LIMIT_SECONDS = 10
ROOM = "room-7"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def place() -> PlaceRef:
    return PlaceRef(kind="call", id=ROOM)


@pytest.fixture
def table() -> PlaceRef:
    return PlaceRef(kind="call", id="table-1")


@pytest.fixture
def app() -> MemberApp:
    return MemberApp()


WIRE_TRACE_LOGGER = "httpcore2.http11"


@pytest.fixture
async def gate(app: MemberApp, caplog: pytest.LogCaptureFixture) -> AsyncIterator[ScriptedGate]:
    async with served(app, caplog) as gate:
        yield gate


@pytest.fixture
def changes() -> ResourceChanges:
    return ResourceChanges(InMemoryPlaceStore())


@pytest.fixture
async def gate_with_changes(
    app: MemberApp, changes: ResourceChanges, caplog: pytest.LogCaptureFixture
) -> AsyncIterator[ScriptedGate]:
    async with served(app, caplog, changes=changes) as gate:
        yield gate


def _without_property(schema: object, name: str) -> None:
    """Removes the property ``name`` from every object schema inside ``schema``."""
    if isinstance(schema, dict):
        schema.get("properties", {}).pop(name, None)
        for value in schema.values():
            _without_property(value, name)
    elif isinstance(schema, list):
        for value in schema:
            _without_property(value, name)


@dataclass
class MemberTools:
    """The member tools an app serves, each declared from the library's own arguments record.

    ``undeclared`` names the arguments the app's tools do not declare, as an app that predates
    them, by path: ``looks[].room`` is the ``room`` of each entry of ``looks``. ``withheld`` is the
    tools the app does not serve. ``outcomes`` is the answer to each next call of a tool, in order,
    where the tool does not answer success; ``calls`` is each call as the wire carried it.
    """

    undeclared: frozenset[str] = frozenset()
    withheld: frozenset[str] = frozenset()
    outcomes: dict[str, list[ToolOutcome]] = field(default_factory=dict[str, list[ToolOutcome]])
    calls: list[tuple[str, JsonObject]] = field(default_factory=list[tuple[str, JsonObject]])

    def served(self) -> list[PersonTool]:
        return [
            PersonTool(self._declaration(name), self._handler(name))
            for name in MEMBER_TOOL_ARGUMENTS
            if name not in self.withheld
        ]

    def called(self, name: str) -> list[JsonObject]:
        return [arguments for tool, arguments in self.calls if tool == name]

    def _declaration(self, name: str) -> ToolDeclaration:
        parameters = tool_parameters(MEMBER_TOOL_ARGUMENTS[name])
        properties = parameters.get("properties", {})
        assert isinstance(properties, dict)
        for path in self.undeclared:
            argument, _, nested = path.partition("[].")
            if nested:
                _without_property(properties.get(argument), nested)
            else:
                properties.pop(argument, None)
        return ToolDeclaration(
            name=name,
            description=f"The member tool {name}.",
            parameters=parameters,
            effect=ToolEffect.SIDE_EFFECTING,
        )

    def _handler(self, name: str) -> Any:
        async def answers(_request: PersonRequest, arguments: JsonObject) -> ToolOutcome:
            self.calls.append((name, arguments))
            if self.outcomes.get(name):
                return self.outcomes[name].pop(0)
            if name == SPEAK_TOOL:
                return ToolOutcome(Spoken(spoken=True).model_dump_json(), is_error=False)
            return ToolOutcome("{}", is_error=False)

        return answers


@pytest.fixture
def bearer(gate: ScriptedGate) -> SecretStr:
    return a_bearer_let_in(gate)


@asynccontextmanager
async def served(
    app: MemberApp, caplog: pytest.LogCaptureFixture, **front: Any
) -> AsyncIterator[ScriptedGate]:
    """The app served behind a gate, whose front takes the keywords, and at the end no log line
    holds a bearer the app was shown."""
    caplog.set_level(0)
    gate = ScriptedGate(app, **front)
    async with gate.serving():
        yield gate
    logged = logged_outside_the_wire_trace(caplog)
    assert not [shown for shown in gate.presented - {""} if shown in logged]


def logged_outside_the_wire_trace(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record but the HTTP library's trace of the response headers.

    That trace shows only what the app echoes, and a host's logger setting is deliberate,
    so the library does not silence it.
    """
    return "\n".join(
        caplog.handler.format(record)
        for record in caplog.records
        if record.name != WIRE_TRACE_LOGGER
    )


def a_bearer_let_in(gate: ScriptedGate) -> SecretStr:
    token = SecretStr(secrets.token_urlsafe(24))
    gate.let_in(token.get_secret_value(), SUBJECT)
    return token


@dataclass
class BearerRenewedAfterOneRead:
    """A host's bearer callable that reads valid until armed; armed, it reads valid once and then
    as a value that is no header value, as a renewal task may replace it between two reads."""

    valid: SecretStr
    armed: bool = False
    _reads_left: int = 0

    def arm(self) -> None:
        self.armed = True
        self._reads_left = 1

    def disarm(self) -> None:
        self.armed = False

    def __call__(self) -> SecretStr:
        if not self.armed:
            return self.valid
        if self._reads_left == 0:
            return SecretStr("renewed\nbadly")
        self._reads_left -= 1
        return self.valid


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


async def reads_the_standing(gate: ScriptedGate, bearer: SecretStr, **keywords: Any) -> None:
    async with opened(gate, bearer, **keywords) as session:
        await session.read_standing()


async def reads_the_window(gate: ScriptedGate, bearer: SecretStr, place: PlaceRef) -> None:
    async with opened(gate, bearer) as session:
        await session.read_place(place)


def answering(status: HTTPStatus) -> Any:
    async def answer(reply: GateReply) -> None:
        await reply.status(status)

    return answer


async def cancelled_once_entered(task: asyncio.Task[Any], entered: asyncio.Event) -> object:
    """What the task ends as when it is cancelled once it has entered the step ``entered`` marks."""
    async with asyncio.timeout(LIMIT_SECONDS):
        await entered.wait()
    task.cancel()
    async with asyncio.timeout(LIMIT_SECONDS):
        (ended,) = await asyncio.gather(task, return_exceptions=True)
    return ended
