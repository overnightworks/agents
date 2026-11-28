"""The member session's stream of wakes: what entering it needs, what it yields in the order the
app sent it, and how it ends.

A wake the real front sends comes from its own change port; a stream that misbehaves is scripted
at the gate.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from agents.mcp_client import (
    MemberSession,
    MemberWake,
    ResourceWake,
    ToolsWake,
)
from agents.mcp_server import ResourceChanges
from agents.member import STANDING_URI
from agents.member_outcomes import LISTEN_NOT_HONORED_CODE, Unreachable, UnusableAnswer
from tests.support import member_session_support
from tests.support.mcp_server_support import MemberApp, ToolOffer, a_member
from tests.support.member_session_support import (
    LIMIT_SECONDS,
    ROOM,
    SUBJECT,
    cancelled_once_entered,
    opened,
    served,
    stands,
)
from tests.support.scripted_gate import (
    LIST_TOOLS,
    LISTEN,
    READ,
    GateReply,
    ScriptedGate,
    acknowledging,
    hangs_once_entered,
    listing,
)

pytestmark = pytest.mark.anyio

anyio_backend = member_session_support.anyio_backend
app = member_session_support.app
bearer = member_session_support.bearer

SHORT_STREAM_SECONDS = 0.3


changes = member_session_support.changes
gate = member_session_support.gate_with_changes


async def wake_of(wakes: AsyncIterator[MemberWake]) -> MemberWake:
    async with asyncio.timeout(LIMIT_SECONDS):
        return await anext(wakes)


async def listens(gate: ScriptedGate, bearer: SecretStr, **keywords: Any) -> None:
    async with opened(gate, bearer, **keywords) as session, session.listen():
        pass


# Entering the stream.


async def test_a_stream_is_open_once_acknowledged_and_a_change_is_one_wake(
    app: MemberApp, gate: ScriptedGate, changes: ResourceChanges, bearer: SecretStr
) -> None:
    stands(app)

    async with opened(gate, bearer) as session, session.listen() as wakes:
        await changes.changed(a_member(SUBJECT), STANDING_URI)
        wake = await wake_of(wakes)
        standing = await session.read_standing()

    assert wake == ResourceWake(STANDING_URI)
    assert [each.place.id for each in standing.standings] == [ROOM]


async def test_an_app_that_does_not_list_the_standing_does_not_honor_the_stream(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    with pytest.raises(UnusableAnswer) as failure:
        await listens(gate, bearer)

    assert failure.value.code == LISTEN_NOT_HONORED_CODE


async def test_a_stream_the_app_completes_without_acknowledging_is_not_honored(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def completes_at_once(reply: GateReply) -> None:
        await reply.open_stream()
        await reply.complete()

    gate.script(LISTEN, completes_at_once)

    with pytest.raises(UnusableAnswer) as failure:
        await listens(gate, bearer)

    assert failure.value.code == LISTEN_NOT_HONORED_CODE


async def test_a_stream_the_app_completes_ends_the_iteration_and_a_new_one_opens_again(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def acknowledges_and_completes(reply: GateReply) -> None:
        await reply.open_stream()
        await reply.acknowledge([STANDING_URI])
        await reply.complete()

    gate.script(LISTEN, acknowledges_and_completes)
    stands_for_the_second = acknowledging(tools=False)
    gate.script(LISTEN, stands_for_the_second)

    async with opened(gate, bearer) as session:
        async with session.listen() as wakes:
            assert [wake async for wake in wakes] == []
        async with session.listen():
            pass


# What the stream yields.


@pytest.mark.parametrize(
    ("sent", "wakes"),
    [
        pytest.param(("tools", "resource"), [ToolsWake(), ResourceWake(ROOM)], id="tools first"),
        pytest.param(("resource", "tools"), [ResourceWake(ROOM), ToolsWake()], id="resource first"),
        pytest.param(
            ("resource", "tools", "resource"),
            [ResourceWake(ROOM), ToolsWake(), ResourceWake(ROOM)],
            id="around a notice",
        ),
    ],
)
async def test_the_wakes_come_in_the_order_the_app_sent_them(
    gate: ScriptedGate,
    bearer: SecretStr,
    sent: tuple[str, ...],
    wakes: list[MemberWake],
) -> None:
    async def writes(reply: GateReply) -> None:
        for each in sent:
            await (reply.tools_notice() if each == "tools" else reply.pulse(ROOM))

    gate.script(LISTEN, acknowledging(tools=True, then=writes))

    async with opened(gate, bearer) as session, session.listen() as stream:
        assert [await wake_of(stream) for _ in sent] == wakes


async def test_a_tools_notice_the_app_did_not_honor_is_no_wake(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def writes(reply: GateReply) -> None:
        await reply.tools_notice()
        await reply.pulse(ROOM)

    gate.script(LISTEN, acknowledging(tools=False, then=writes))

    async with opened(gate, bearer) as session, session.listen() as stream:
        wake = await wake_of(stream)
        notices = session.tools_notices()

    assert (wake, notices) == (ResourceWake(ROOM), 0)


async def test_a_prompts_notice_the_app_honored_is_no_wake(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def writes(reply: GateReply) -> None:
        await reply.prompts_notice()
        await reply.pulse(ROOM)

    gate.script(LISTEN, acknowledging(tools=False, prompts=True, then=writes))

    async with opened(gate, bearer) as session, session.listen() as stream:
        wake = await wake_of(stream)

    assert wake == ResourceWake(ROOM)


async def test_an_error_the_callers_own_body_raises_in_the_listen_is_not_told_as_a_failure(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    gate.script(LISTEN, acknowledging(tools=False))

    async with opened(gate, bearer) as session:
        with pytest.raises(OSError, match="the host's own") as raised:
            await raises_inside_the_listen(session)

    assert type(raised.value) is OSError


async def raises_inside_the_listen(session: Any) -> None:
    async with session.listen():
        raise OSError("the host's own")


async def kept_after_listing(session: MemberSession) -> bool:
    await session.declared_tools()
    return session.kept_tools() is not None


@pytest.mark.parametrize("honored", [True, False], ids=["honored", "not honored"])
async def test_a_listing_is_kept_only_while_a_stream_that_honored_the_tools_notice_is_open(
    gate: ScriptedGate, bearer: SecretStr, honored: bool
) -> None:
    gate.script(LISTEN, acknowledging(tools=honored))
    gate.script(LIST_TOOLS, listing("one"), listing("one"))

    async with opened(gate, bearer) as session:
        async with session.listen():
            kept_in_the_stream = await kept_after_listing(session)
        kept_after_it = session.kept_tools()

    assert (kept_in_the_stream, kept_after_it) == (honored, None)


async def test_a_refused_listing_leaves_no_kept_list(gate: ScriptedGate, bearer: SecretStr) -> None:
    gate.script(LISTEN, acknowledging(tools=True))
    gate.script(LIST_TOOLS, listing("one", "one"))

    async with opened(gate, bearer) as session:
        async with session.listen():
            with pytest.raises(UnusableAnswer):
                await session.declared_tools()
            kept = session.kept_tools()

    assert kept is None


async def test_a_tools_notice_drops_the_kept_list_and_counts(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    notice_may_come = asyncio.Event()

    async def notifies_when_told(reply: GateReply) -> None:
        await notice_may_come.wait()
        await reply.tools_notice()

    gate.script(LISTEN, acknowledging(tools=True, then=notifies_when_told))
    gate.script(LIST_TOOLS, listing("one"))

    async with opened(gate, bearer) as session, session.listen() as stream:
        await session.declared_tools()
        kept = session.kept_tools()
        notice_may_come.set()
        wake = await wake_of(stream)
        after = (session.kept_tools(), session.tools_notices())

    assert kept is not None
    assert wake == ToolsWake()
    assert after == (None, 1)


async def test_a_tools_notice_during_a_listing_keeps_no_list(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    listing_started, notice_may_come = asyncio.Event(), asyncio.Event()

    async def notifies_while_listing(reply: GateReply) -> None:
        await notice_may_come.wait()
        await reply.tools_notice()

    async def answers_the_listing_after_the_notice(reply: GateReply) -> None:
        listing_started.set()
        async with asyncio.timeout(LIMIT_SECONDS):
            await notice_taken.wait()
        await listing("one")(reply)

    notice_taken = asyncio.Event()
    gate.script(LISTEN, acknowledging(tools=True, then=notifies_while_listing))
    gate.script(LIST_TOOLS, answers_the_listing_after_the_notice)

    async with opened(gate, bearer) as session, session.listen() as stream:
        listed = asyncio.create_task(session.declared_tools())
        async with asyncio.timeout(LIMIT_SECONDS):
            await listing_started.wait()
        notice_may_come.set()
        await wake_of(stream)
        notice_taken.set()
        await listed
        kept = session.kept_tools()

    assert kept is None


# How the stream ends.


async def test_a_stream_the_app_cuts_ends_the_iteration_as_unreachable(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def cuts_after_acknowledging(reply: GateReply) -> None:
        await reply.open_stream()
        await reply.acknowledge([STANDING_URI])
        await reply.cut()

    gate.script(LISTEN, cuts_after_acknowledging)

    async def reads_the_stream() -> None:
        async with opened(gate, bearer) as session, session.listen() as stream:
            async for _ in stream:
                pass

    with pytest.raises(Unreachable):
        await reads_the_stream()


async def test_traffic_with_no_acknowledgement_until_past_the_deadline_is_unreachable(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    async def talks_without_acknowledging(reply: GateReply) -> None:
        await reply.open_stream()
        while True:
            await reply.keep_alive()
            await asyncio.sleep(0.01)

    gate.script(LISTEN, talks_without_acknowledging)

    with pytest.raises(Unreachable):
        await listens(gate, bearer, stream_seconds=SHORT_STREAM_SECONDS)


async def test_a_stream_that_outlasts_its_deadline_by_sending_on_is_unreachable(
    app: MemberApp, gate: ScriptedGate, bearer: SecretStr
) -> None:
    stands(app)

    async def sends_the_next_once_the_last_was_read(reply: GateReply) -> None:
        await reply.open_stream()
        await reply.acknowledge([STANDING_URI])
        reads = 0
        while True:
            await reply.pulse(STANDING_URI)
            reads += 1
            while gate.count(READ) < reads:
                await asyncio.sleep(0)

    gate.script(LISTEN, sends_the_next_once_the_last_was_read)

    async def reads_the_standing_for_each_pulse() -> None:
        async with (
            opened(gate, bearer, stream_seconds=SHORT_STREAM_SECONDS) as session,
            session.listen() as stream,
        ):
            async for _ in stream:
                await session.read_standing()

    with pytest.raises((Unreachable, ExceptionGroup)) as caught:
        await reads_the_standing_for_each_pulse()

    leaves = caught.value.exceptions if isinstance(caught.value, ExceptionGroup) else [caught.value]
    assert all(isinstance(leaf, Unreachable) for leaf in leaves)
    assert gate.count(READ) > 1


async def test_a_task_cancelled_before_the_acknowledgement_ends_as_the_cancellation(
    gate: ScriptedGate, bearer: SecretStr
) -> None:
    opened_without_acknowledging = asyncio.Event()

    async def opens_and_waits(reply: GateReply) -> None:
        await reply.open_stream()
        await hangs_once_entered(opened_without_acknowledging)(reply)

    gate.script(LISTEN, opens_and_waits)
    task = asyncio.create_task(listens(gate, bearer))

    ended = await cancelled_once_entered(task, opened_without_acknowledging)

    assert isinstance(ended, asyncio.CancelledError)


async def test_a_change_of_the_tools_the_real_front_offers_is_a_tools_wake(
    app: MemberApp,
    changes: ResourceChanges,
    caplog: pytest.LogCaptureFixture,
) -> None:
    offer = ToolOffer(offered={a_member(SUBJECT): ["note"]})
    stands(app)
    async with served(app, caplog, changes=changes, offered_tools=offer) as announcing:
        subject_bearer = member_session_support.a_bearer_let_in(announcing)
        async with opened(announcing, subject_bearer) as session, session.listen() as wakes:
            await changes.tools_changed(a_member(SUBJECT))
            wake = await wake_of(wakes)

    assert wake == ToolsWake()
