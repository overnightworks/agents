"""A runner's session against an app's member endpoint: one SDK session over the shared HTTP policy.

The session writes no log line, decides nothing and holds no deployment fact: the URL, the bearer
and the transport come from the host, and it reports an argument it leaves out through the host's
callback. Every call that fails raises a ``CallFailure`` of ``agents.member_outcomes``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, override

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import listen as subscribe
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    CallToolResult,
    InputRequiredResult,
    ReadResourceResult,
    Request,
    RequestParams,
    TextResourceContents,
)
from pydantic import SecretStr, ValidationError

from agents.http_client import policy_client
from agents.mcp_client.arguments import (
    ACCEPT_OPTIONAL,
    DECISION_OPTIONAL,
    SPEAK_OPTIONAL,
    TURN_STATE_OPTIONAL,
    declares,
    split_undeclared,
)
from agents.mcp_client.exchange import CALL_ANSWER, BearerAuth, CallAnswer
from agents.mcp_client.fold import (
    CALL_ERRORS,
    InvalidToolResult,
    failure_of,
    folded,
    leaves_of,
    task_is_being_cancelled,
)
from agents.mcp_client.results import (
    accept_refusal_of,
    declaration_of,
    decline_refusal_of,
    naming_hold,
    picture_of,
    record_of,
    refusal_in,
    speak_answer_of,
    text_of,
)
from agents.mcp_client.wakes import MemberWake, wakes_of
from agents.member import (
    ACCEPT_INVITATION_TOOL,
    CONTINUE_SPEAKING_TOOL,
    DECLINE_INVITATION_TOOL,
    LISTEN_BOUND_SECONDS,
    SET_TURN_STATE_TOOL,
    SPEAK_TOOL,
    STANDING_URI,
    STOP_SPEAKING_TOOL,
    AcceptInvitationArguments,
    AcceptInvitationRefusal,
    ContinueSpeakingRefusal,
    ContinueSpeakingRefused,
    DeclineInvitationArguments,
    DeclineInvitationRefusal,
    MemberResourceRecord,
    MemberStanding,
    PlaceRef,
    PlaceView,
    PlaceWindow,
    SetTurnStateArguments,
    SetTurnStateRefusal,
    SetTurnStateRefused,
    SpeakAnswer,
    SpeakRefusal,
    StopSpeakingRefusal,
    StopSpeakingRefused,
    place_view_uri,
    place_window_uri,
)
from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    LISTEN_NOT_HONORED_CODE,
    UNSUPPORTED_VERSION_CODE,
    ArgumentLeftOut,
    CallFailure,
    Delivery,
    Unreachable,
    UnrecognisedRefusal,
    UnusableAnswer,
)
from agents.member_speaking import require_the_hold_the_sentence_answers
from agents.skills import (
    SKILLS_EXTENSION,
    SKILLS_GET_METHOD,
    SKILLS_LIST_METHOD,
    Skill,
    SkillAnswer,
    SkillListing,
    SkillsGetParams,
    SkillsListParams,
    read_skill,
)
from agents.tools import JsonObject, ToolDeclaration, ToolOutcome

CONNECT_SECONDS: Final = 30.0
READ_SECONDS: Final = 2 * LISTEN_BOUND_SECONDS
"""Twice the bound an app puts on a stream: the runner cuts a stream that outlasts it."""


@dataclass(frozen=True)
class AppDiscovery:
    """What the app said of itself when the session found its protocol revision."""

    instructions: str | None
    declares_skills: bool


class MemberSession(Protocol):
    """What a runner reads of an app and asks of it; each call fails with a ``StatusFailure``,
    ``Unreachable`` or ``UnusableAnswer``, except a transport failure.

    A transport failure of a call, such as a request the app leaves unanswered for
    ``stream_seconds``, reaches the call as a bare ``asyncio.CancelledError``; the ``Unreachable``
    is raised when the session ends.
    """

    def listen(self) -> AbstractAsyncContextManager[AsyncIterator[MemberWake]]:
        """A stream of wakes; entering returns once the app acknowledged it with the standing
        honored.

        Iterating yields a wake for each change of a resource or of the app's tools, in the order
        the app sent them, and ends when the app ends the stream. The stream, its acknowledgement
        and everything the body does share one deadline.
        """
        ...

    async def read_standing(self) -> MemberStanding:
        """Every standing of the member."""
        ...

    async def read_place(self, place: PlaceRef) -> PlaceWindow | None:
        """The place's window, or ``None`` when the app shows it to this member no more."""
        ...

    async def read_view(self, place: PlaceRef) -> PlaceView | None:
        """The place's view, or ``None`` when the app shows it to this member no more."""
        ...

    async def read_discovery(self) -> AppDiscovery:
        """What the app said of itself; no request."""
        ...

    async def declared_tools(self) -> tuple[ToolDeclaration, ...]:
        """The first page of the app's tools."""
        ...

    def kept_tools(self) -> tuple[ToolDeclaration, ...] | None:
        """The list ``declared_tools`` kept, or ``None`` when there is none.

        A list is kept while a stream that honoured the app's tools notice is open and no notice,
        acknowledgement or stream end came after the listing started.
        """
        ...

    def tools_notices(self) -> int:
        """How many tools notices this session received."""
        ...

    async def invoke(self, name: str, arguments: JsonObject) -> ToolOutcome:
        """The tool's outcome, an error one included.

        A name the app never declared is no outcome: the app answers 400, which raises
        ``StatusFailure``. A declared tool outside the app's current offer returns an error
        ``ToolOutcome``, so a runner that keeps a tool list handles both.
        """
        ...

    async def accept(
        self, arguments: AcceptInvitationArguments
    ) -> AcceptInvitationRefusal | UnrecognisedRefusal | None:
        """``None`` when joined, else the refusal."""
        ...

    async def decline(
        self, arguments: DeclineInvitationArguments
    ) -> Delivery | DeclineInvitationRefusal | UnrecognisedRefusal:
        """Declines an invitation, unless the app lists no tool to decline with, which leaves the
        invitation to run out (``Delivery.NOT_SERVED``); the refusal says the invitation is
        gone."""
        ...

    async def speak(
        self,
        place: PlaceRef,
        sentence: str,
        style: str | None = None,
        interrupted: str | None = None,
        *,
        keeps_queue: bool = False,
        told_changes: int | None = None,
    ) -> SpeakAnswer | SpeakRefusal:
        """That the sentence starts to play, that the app wrote it at the place instead, or why it
        does not speak it.

        ``style`` is how it sounds, passed on unchanged; ``interrupted`` names the hold the
        sentence answers instead of the held rest; ``keeps_queue``, with ``interrupted``, keeps
        the queue behind the sentence. An app whose ``speak`` does not declare ``keeps_queue``
        is sent the call without it, the tools listed first, and the queue then answers
        ``stopped``. ``told_changes``, with ``interrupted``, is the ``Interrupted.worded_changes``
        the decision was told; an app that declares none is sent the call without it, and the app
        refuses ``stale`` a sentence told fewer than it counted. A sentence that names a hold
        answers when it plays, so its answer can come late and as ``not_interrupted``.
        ``keeps_queue`` or ``told_changes`` without ``interrupted`` raises ``ValueError`` before
        anything is sent, against every app. An answer that is none of these is an
        ``UnusableAnswer``.
        """
        ...

    async def continue_speaking(
        self,
        place: PlaceRef,
        interrupted: str,
        *,
        finish: bool = False,
        told_changes: int | None = None,
    ) -> ContinueSpeakingRefusal | Literal[Delivery.NOT_SERVED] | None:
        """``None`` when the held sentence goes on, else the refusal.

        The answer comes when the sentence plays on, which waits until no person of the hold's
        talk-over speaks, so it can come late and as ``not_interrupted``. With ``finish``, only
        the held sentence's rest plays and the queue is dropped. It is sent only to an app whose
        ``continue_speaking`` declares ``finish``, the tools listed first; otherwise nothing is
        sent and the outcome is ``Delivery.NOT_SERVED``. ``told_changes`` is the
        ``Interrupted.worded_changes`` the decision was told; an app that declares none is sent
        the call without it, and the app refuses ``stale`` a call told fewer than it counted.
        """
        ...

    async def stop_speaking(
        self, place: PlaceRef, interrupted: str, *, told_changes: int | None = None
    ) -> StopSpeakingRefusal | None:
        """``None`` when the hold ended with nothing more said, else the refusal.

        ``told_changes`` is the ``Interrupted.worded_changes`` the decision was told; an app that
        declares none is sent the call without it, and the app refuses ``stale`` a call told
        fewer than it counted. A stop that ends no decision of its own names none."""
        ...

    async def set_turn_state(self, statement: SetTurnStateArguments) -> SetTurnStateRefusal | None:
        """``None`` when the app took what the member's turns at the place do, else the refusal."""
        ...

    async def list_skills(self, cursor: str | None = None, *, listed: int = 0) -> SkillListing:
        """One page of the app's skills, from the start or from ``cursor``.

        A page that breaks a rule is an ``UnusableAnswer`` whose ``entry`` is the index, counting
        the ``listed`` entries of the pages before, of the entry that did.
        """
        ...

    async def get_skill(self, uri: str) -> Skill:
        """The skill whose ``SKILL.md`` is at ``uri``, once the file proves to be what the app's
        entry promised; raises ``SkillRefused`` for a file that is not."""
        ...


class _UnknownResource(Exception):  # noqa: N818
    """The app shows this member no such resource."""


class _ListParams(RequestParams, SkillsListParams):
    """The params record with the ``_meta`` member the SDK stamps on every request."""


class _GetParams(RequestParams, SkillsGetParams):
    """The params record with the ``_meta`` member the SDK stamps on every request."""


class _SkillsListRequest(Request[_ListParams, Literal["skills/list"]]):
    method: Literal["skills/list"] = SKILLS_LIST_METHOD
    params: _ListParams


class _SkillsGetRequest(Request[_GetParams, Literal["skills/get"]]):
    method: Literal["skills/get"] = SKILLS_GET_METHOD
    params: _GetParams


class _ValidatingSession(ClientSession):
    """Names the SDK's validation failure, which it raises as a bare ``RuntimeError``."""

    @override
    async def validate_tool_result(self, name: str, result: CallToolResult) -> None:
        try:
            await super().validate_tool_result(name, result)
        except RuntimeError:
            raise InvalidToolResult from None


@dataclass
class _McpMemberSession:
    session: ClientSession
    auth: BearerAuth = field(repr=False)
    url: str
    stream_seconds: float
    on_argument_left_out: Callable[[ArgumentLeftOut], None] | None
    _honoured: bool = False
    """Whether the open stream was acknowledged with the tools notice honoured."""
    _list_counter: int = 0
    """Moves at each acknowledgement, each tools notice and each stream end; a listing that started
    before it moved is not kept."""
    _notices: int = 0
    _kept: tuple[ToolDeclaration, ...] | None = None
    _declared_arguments: dict[str, JsonObject] | None = None
    """The input schema of each tool of the app's latest list; a list moves it with the tools
    notice."""
    _left_out: set[ArgumentLeftOut] = field(default_factory=set[ArgumentLeftOut])
    """The left-out arguments already reported in this session."""

    @asynccontextmanager
    async def listen(self) -> AsyncGenerator[AsyncIterator[MemberWake]]:
        self.auth.last_answer = None
        deadline = asyncio.timeout(self.stream_seconds)
        raised_in_body: Exception | None = None
        try:
            async with deadline, AsyncExitStack() as opened:
                with self.auth.pinned_bearer():
                    subscription = await opened.enter_async_context(
                        subscribe(
                            self.session,
                            resource_subscriptions=[STANDING_URI],
                            tools_list_changed=True,
                        )
                    )
                if STANDING_URI not in (subscription.honored.resource_subscriptions or ()):
                    raise UnusableAnswer(LISTEN_NOT_HONORED_CODE)
                self._move_list_counter(honoured=bool(subscription.honored.tools_list_changed))
                try:
                    yield wakes_of(subscription, self.auth, self._tools_notice)
                except Exception as error:
                    raised_in_body = error
                    raise
        except CallFailure:
            raise
        except CALL_ERRORS as error:
            if error is raised_in_body:
                raise
            if isinstance(error, TimeoutError) and deadline.expired():
                raise Unreachable("the stream outlasted its deadline") from None
            raise failure_of(error, self.auth.last_answer) from None
        finally:
            self._move_list_counter(honoured=False)

    def _move_list_counter(self, *, honoured: bool) -> None:
        self._honoured = honoured
        self._list_counter += 1
        self._kept = None
        self._declared_arguments = None

    def _tools_notice(self) -> None:
        self._notices += 1
        self._move_list_counter(honoured=self._honoured)

    def kept_tools(self) -> tuple[ToolDeclaration, ...] | None:
        return self._kept

    def tools_notices(self) -> int:
        return self._notices

    async def read_standing(self) -> MemberStanding:
        return record_of(MemberStanding, await self._asked(self._read(STANDING_URI)))

    async def read_place(self, place: PlaceRef) -> PlaceWindow | None:
        return await self._read_record(PlaceWindow, place_window_uri(place))

    async def read_view(self, place: PlaceRef) -> PlaceView | None:
        return await self._read_record(PlaceView, place_view_uri(place))

    async def read_discovery(self) -> AppDiscovery:
        discovered = await self.session.discover()
        extensions = discovered.capabilities.extensions or {}
        return AppDiscovery(discovered.instructions, SKILLS_EXTENSION in extensions)

    async def _read_record[Record: MemberResourceRecord](
        self, record: type[Record], uri: str
    ) -> Record | None:
        """The record at the resource, or ``None`` when the app shows this member no such one."""
        read = self._read(uri, unknown_is_absent=True)
        try:
            return record_of(record, await self._asked(read))
        except _UnknownResource:
            return None

    async def declared_tools(self) -> tuple[ToolDeclaration, ...]:
        started = self._list_counter
        listing = await self._asked(self.session.list_tools())
        try:
            declared = tuple(declaration_of(tool) for tool in listing.tools)
        except ValidationError:
            raise UnusableAnswer(INVALID_ANSWER_CODE) from None
        if len({tool.name for tool in declared}) != len(declared):
            raise UnusableAnswer(INVALID_ANSWER_CODE)
        if self._list_counter == started:
            self._declared_arguments = {tool.name: tool.parameters for tool in declared}
            if self._honoured:
                self._kept = declared
        return declared

    async def invoke(self, name: str, arguments: JsonObject) -> ToolOutcome:
        result = await self._called(name, arguments)
        return ToolOutcome(text_of(result), result.is_error, picture_of(result))

    async def accept(
        self, arguments: AcceptInvitationArguments
    ) -> AcceptInvitationRefusal | UnrecognisedRefusal | None:
        result = await self._called_declared(
            ACCEPT_INVITATION_TOOL,
            arguments.model_dump(mode="json"),
            optional=ACCEPT_OPTIONAL,
            lists=True,
        )
        return accept_refusal_of(text_of(result)) if result.is_error else None

    async def decline(
        self, arguments: DeclineInvitationArguments
    ) -> Delivery | DeclineInvitationRefusal | UnrecognisedRefusal:
        if DECLINE_INVITATION_TOOL not in await self._declared_parameters():
            return Delivery.NOT_SERVED
        result = await self._called(DECLINE_INVITATION_TOOL, arguments.model_dump(mode="json"))
        if result.is_error:
            return decline_refusal_of(text_of(result))
        return Delivery.SENT

    async def speak(
        self,
        place: PlaceRef,
        sentence: str,
        style: str | None = None,
        interrupted: str | None = None,
        *,
        keeps_queue: bool = False,
        told_changes: int | None = None,
    ) -> SpeakAnswer | SpeakRefusal:
        require_the_hold_the_sentence_answers(
            interrupted=interrupted, keeps_queue=keeps_queue, told_changes=told_changes
        )
        # The arguments are the wire shape of ``SpeakArguments``, built unvalidated: a refusal of a
        # sentence is the app's, and a validation error would carry the sentence.
        arguments: JsonObject = {"place": place.model_dump(mode="json"), "text": sentence}
        if style is not None:
            arguments["style"] = style
        if interrupted is not None:
            arguments["interrupted"] = interrupted
        if keeps_queue:
            arguments["keeps_queue"] = True
        if told_changes is not None:
            arguments["told_changes"] = told_changes
        result = await self._called_declared(
            SPEAK_TOOL, arguments, optional=SPEAK_OPTIONAL, lists=keeps_queue
        )
        return speak_answer_of(result)

    async def continue_speaking(
        self,
        place: PlaceRef,
        interrupted: str,
        *,
        finish: bool = False,
        told_changes: int | None = None,
    ) -> ContinueSpeakingRefusal | Literal[Delivery.NOT_SERVED] | None:
        if finish and not declares(
            (await self._declared_parameters()).get(CONTINUE_SPEAKING_TOOL), "finish"
        ):
            return Delivery.NOT_SERVED
        result = await self._called_declared(
            CONTINUE_SPEAKING_TOOL,
            naming_hold(place, interrupted, finish=finish, told_changes=told_changes),
            optional=DECISION_OPTIONAL,
        )
        return refusal_in(ContinueSpeakingRefused, result).refused if result.is_error else None

    async def stop_speaking(
        self, place: PlaceRef, interrupted: str, *, told_changes: int | None = None
    ) -> StopSpeakingRefusal | None:
        result = await self._called_declared(
            STOP_SPEAKING_TOOL,
            naming_hold(place, interrupted, told_changes=told_changes),
            optional=DECISION_OPTIONAL,
        )
        return refusal_in(StopSpeakingRefused, result).refused if result.is_error else None

    async def set_turn_state(self, statement: SetTurnStateArguments) -> SetTurnStateRefusal | None:
        result = await self._called_declared(
            SET_TURN_STATE_TOOL, statement.model_dump(mode="json"), optional=TURN_STATE_OPTIONAL
        )
        return refusal_in(SetTurnStateRefused, result).refused if result.is_error else None

    async def list_skills(self, cursor: str | None = None, *, listed: int = 0) -> SkillListing:
        return await self._asked(self._skills_page(cursor, listed))

    async def _skills_page(self, cursor: str | None, listed: int) -> SkillListing:
        try:
            return await self.session.send_request(
                _SkillsListRequest(params=_ListParams(cursor=cursor)), SkillListing
            )
        except ValidationError as error:
            raise UnusableAnswer(INVALID_ANSWER_CODE, entry=_listed_index(error, listed)) from None

    async def get_skill(self, uri: str) -> Skill:
        entry = (
            await self._asked(
                self.session.send_request(
                    _SkillsGetRequest(params=_GetParams(uri=uri)), SkillAnswer
                )
            )
        ).skill
        if entry.uri != uri:
            raise UnusableAnswer(INVALID_ANSWER_CODE)
        result = await self._asked(self._read(entry.uri))
        content = "".join(
            block.text for block in result.contents if isinstance(block, TextResourceContents)
        )
        return read_skill(self.url, entry, content.encode())

    async def _called(self, name: str, arguments: JsonObject) -> CallToolResult:
        result = await self._asked(
            self.session.call_tool(name, arguments, allow_input_required=True)
        )
        if not isinstance(result, CallToolResult):
            raise UnusableAnswer(INVALID_ANSWER_CODE)
        return result

    async def _declared_parameters(self) -> dict[str, JsonObject]:
        """The parameters each tool declares: the latest list, or one asked for when none stands."""
        if self._declared_arguments is not None:
            return self._declared_arguments
        return {tool.name: tool.parameters for tool in await self.declared_tools()}

    async def _called_declared(
        self, name: str, arguments: JsonObject, *, optional: frozenset[str], lists: bool = False
    ) -> CallToolResult:
        """``_called`` without the ``optional`` arguments its tool's declared schema does not name.

        The schema is the app's latest list as ``declared_tools`` read it. A call that carries an
        optional argument and has none yet asks for the list only when it ``lists``, and then
        reads the parameters ``_declared_parameters`` decides; a turn's own statements do not,
        since the turn lists beside them. An argument left out is reported once per session.
        """
        if optional.isdisjoint(arguments):
            return await self._called(name, arguments)
        declared = await self._declared_parameters() if lists else self._declared_arguments
        schema = (declared or {}).get(name)
        if schema is None:
            return await self._called(name, arguments)
        sent, left_out = split_undeclared(arguments, schema, optional)
        for argument in left_out:
            self._report_left_out(ArgumentLeftOut(name, argument))
        return await self._called(name, sent)

    def _report_left_out(self, left_out: ArgumentLeftOut) -> None:
        if left_out not in self._left_out:
            self._left_out.add(left_out)
            if self.on_argument_left_out is not None:
                self.on_argument_left_out(left_out)

    async def _read(self, uri: str, *, unknown_is_absent: bool = False) -> ReadResourceResult:
        """The resource; an app that shows this member no such one answers an invalid-params
        error."""
        try:
            result = await self.session.read_resource(uri, allow_input_required=True)
        except MCPError as error:
            if unknown_is_absent and error.code == INVALID_PARAMS:
                raise _UnknownResource from None
            raise
        if isinstance(result, InputRequiredResult):
            raise UnusableAnswer(INVALID_ANSWER_CODE)
        return result

    async def _asked[Answer](self, request: Coroutine[object, object, Answer]) -> Answer:
        """The call's answer; a failure is told by the call's own last HTTP answer, never by one to
        a call beside it. A bearer that is no header value fails the call before its request."""
        try:
            with self.auth.pinned_bearer():
                return await self._answered(request)
        finally:
            request.close()

    async def _answered[Answer](self, request: Coroutine[object, object, Answer]) -> Answer:
        self.auth.last_answer = None
        stranding = asyncio.timeout(None)
        call = CallAnswer(stranding)
        reset = CALL_ANSWER.set(call)
        try:
            async with stranding:
                return await request
        except CALL_ERRORS as error:
            raise failure_of(error, call.answer) from None
        finally:
            CALL_ANSWER.reset(reset)


def _listed_index(error: ValidationError, listed: int) -> int | None:
    """The index, among all entries listed so far and on this page, of the first entry the error
    names."""
    for detail in error.errors():
        location = detail["loc"]
        if len(location) > 1 and location[0] == "skills" and isinstance(location[1], int):
            return listed + location[1]
    return None


@asynccontextmanager
async def open_member_session(
    url: str,
    bearer: Callable[[], SecretStr],
    *,
    on_argument_left_out: Callable[[ArgumentLeftOut], None] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    stream_seconds: float = READ_SECONDS,
) -> AsyncGenerator[MemberSession]:
    """A session at the app's revision; the failures a call raises are the ones ``MemberSession``
    names.

    ``bearer`` is read once for each call, so a renewed token reaches the next call; a token that is
    no header value fails the call before its request is sent. ``on_argument_left_out`` is told once
    per session of each optional argument the app's tool does not declare. A bare ``ClientSession``
    discovers the revision: the SDK's own client would fall back to ``initialize`` on any error, so
    a 401 on the probe would enter a path this runner does not have. Several leaves, failures or
    defects, leave as one ``ExceptionGroup`` holding each of them. A session whose task was being
    cancelled while it discovered the revision or when the body left, and that closes with late
    answers alone, raises the cancellation: the SDK's task groups lower the task's cancellation
    count and turn it into those answers.
    """
    auth = BearerAuth(bearer)
    timeout = httpx2.Timeout(CONNECT_SECONDS, read=stream_seconds)
    raised_in_body: Exception | None = None
    cancelled_in_body = False
    outcome: Exception | asyncio.CancelledError | None = None
    try:
        async with (
            policy_client(timeout=timeout, transport=transport, auth=auth) as client,
            streamable_http_client(url, http_client=client) as (read_stream, write_stream),
            _ValidatingSession(read_stream, write_stream) as session,
        ):
            try:
                await _discover(session, auth)
            except asyncio.CancelledError:
                cancelled_in_body = task_is_being_cancelled()
                raise
            try:
                yield _McpMemberSession(session, auth, url, stream_seconds, on_argument_left_out)
            except Exception as error:
                raised_in_body = error
                raise
            except asyncio.CancelledError:
                cancelled_in_body = task_is_being_cancelled()
                raise
    except Exception as error:
        folded_error = folded(error, raised_in_body, auth, cancelled_in_body=cancelled_in_body)
        if not _is_leaf_of(folded_error, error, raised_in_body):
            raise folded_error from None
        outcome = folded_error
    finally:
        if outcome is not None:
            # Raised outside the handler, a leaf the body raised keeps its cause and its context;
            # the group of the caller's exit that is being handled would otherwise replace it.
            context = outcome.__context__
            try:
                raise outcome
            finally:
                outcome.__context__ = context


def _is_leaf_of(
    folded_error: Exception | asyncio.CancelledError,
    error: Exception,
    raised_in_body: Exception | None,
) -> bool:
    """Whether the folded error is one of the leaves the session's exit or its body raised."""
    raised = () if raised_in_body is None else leaves_of(raised_in_body)
    return any(folded_error is leaf for leaf in (*leaves_of(error), *raised))


async def _discover(session: ClientSession, auth: BearerAuth) -> None:
    auth.last_answer = None
    try:
        await session.discover()
    except RuntimeError:
        raise UnusableAnswer(UNSUPPORTED_VERSION_CODE) from None
    except CALL_ERRORS as error:
        raise failure_of(error, auth.last_answer) from None
