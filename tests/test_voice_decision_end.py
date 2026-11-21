"""The voice loop reports one decision end per decision, once what the decision caused is done."""

import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, Reply, completion
from agents.turn import RejectionCode
from agents.voice import (
    DecisionEnded,
    DecisionFailed,
    HoldEnded,
    HoldEnding,
    ReplyUnreadable,
    SentenceRefused,
    StayedSilent,
    VoiceRefused,
    Woke,
)
from tests.support import voice_support
from tests.support.voice_lines import a_line
from tests.support.voice_support import (
    A_GATEWAY_REFUSAL,
    A_REFUSED_TURN,
    A_SILENT_ROUND,
    RETURNED,
    PacedGateway,
    RecordingVoice,
    a_lookup_tool,
    an_answer_of_two_that_is_playing,
    finish,
    hang,
    piece,
    the_hold_ends,
)

place = voice_support.place


def test_a_decision_reports_its_end_once_its_last_sentence_has_played(place):
    trace = place.trace
    voice = RecordingVoice(trace, seconds_per_sentence=5)
    gateway = PacedGateway(trace, [piece("Eins. Zwei."), finish()], [piece("Gern."), finish()])

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield a_line(2, "Und?")
        await trace.until(lambda: trace.count(DecisionEnded()) == 2)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.order_of(Woke(), DecisionEnded(), ("played", "Zwei."), ("played", "Gern.")) == [
        Woke(),
        ("played", "Zwei."),
        DecisionEnded(),
        Woke(),
        ("played", "Gern."),
        DecisionEnded(),
    ]


@pytest.mark.parametrize(
    ("reply", "report"),
    [
        (completion("[silent]"), StayedSilent()),
        (completion("[shout] Hallo."), ReplyUnreadable()),
        (A_GATEWAY_REFUSAL, A_REFUSED_TURN),
    ],
)
def test_a_silent_unreadable_or_failed_decision_reports_its_end_once(place, reply, report):
    gateway = FakeGateway(reply)

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert place.trace.events == [Woke(), report, DecisionEnded()]


@pytest.mark.parametrize(
    "refusal", [{("play", "Eins."): VoiceRefused()}, {("render", "Zwei."): VoiceRefused()}]
)
def test_a_refused_sound_reports_the_end_after_the_turn_was_closed(place, refusal):
    trace = place.trace
    voice = RecordingVoice(trace, raises=refusal)
    gateway = PacedGateway(trace, [piece("Eins. Zwei. Drei. Vier"), hang])

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    refused = next(event for event in trace.events if isinstance(event, SentenceRefused))
    assert trace.order_of(refused, ("closed",), DecisionEnded()) == [
        refused,
        ("closed",),
        DecisionEnded(),
    ]


def test_a_decision_reports_its_end_only_after_its_tools_have_run(place):
    gateway = FakeGateway(A_SILENT_ROUND, completion("[silent]"))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        for _ in range(3):
            await trace.settled()

    tools = [a_lookup_tool(place.trace, takes_a_moment=True)]
    assert place.run(hearing, gateway, tools=tools) == RETURNED

    assert place.trace.order_of(("tool", "answered"), DecisionEnded()) == [
        ("tool", "answered"),
        DecisionEnded(),
    ]


@pytest.mark.parametrize(
    ("reply", "wait"),
    [(Reply(429, {"Retry-After": "7"}), 7), (Reply(429), None)],
)
def test_a_busy_refusal_reports_its_wait_on_the_failed_decision(place, reply, wait):
    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()

    assert place.run(hearing, FakeGateway(reply)) == RETURNED

    assert place.trace.events == [
        Woke(),
        DecisionFailed(TurnStopKind.REFUSED, RejectionCode.BUSY, None, retry_after_s=wait),
        DecisionEnded(),
    ]


@pytest.mark.parametrize(
    ("reply", "last_played"),
    [("[continue]", "Zwei."), ("[finish]", "Eins."), ("Gut.", "Gut.")],
)
def test_an_answer_with_a_hold_reports_two_woke_and_one_end_after_its_ending_has_played(
    place, reply, last_played
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion(reply))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    trace = place.trace
    assert trace.events.count(Woke()) == 2
    assert trace.events.count(DecisionEnded()) == 1
    assert trace.events[-1] == DecisionEnded()
    assert trace.entries.index(("played", last_played)) < trace.entries.index(DecisionEnded())


@pytest.mark.parametrize(
    ("reply", "ending"),
    [(completion("[silent]"), HoldEnding.STOPPED), (A_GATEWAY_REFUSAL, HoldEnding.DROPPED)],
)
def test_a_stopping_or_dropping_hold_reports_the_end_after_the_paused_sentence_was_stopped(
    place, reply, ending
):
    voice, gateway = an_answer_of_two_that_is_playing(place, reply)

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    trace = place.trace
    assert trace.events.count(DecisionEnded()) == 1
    assert trace.order_of(HoldEnded(ending), ("stopped", "Eins."), DecisionEnded())[-1] == (
        DecisionEnded()
    )
