"""A hold ends in one decision, and a newer talk-over waits for that ending."""

import pytest

from agents.backend.fake_gateway import FakeGateway, completion
from agents.voice import (
    DecisionEnded,
    Held,
    HoldEnded,
    HoldEnding,
    ReplyUnreadable,
    SentenceRefused,
    SentenceStarted,
    StayedSilent,
    VoiceRefused,
    Woke,
)
from tests.support import voice_support
from tests.support.voice_lines import a_line, speaking
from tests.support.voice_support import (
    HELD_AT_ONE,
    RETURNED,
    THE_FIRST_ANSWER,
    PacedGateway,
    RecordingVoice,
    a_look_up_call,
    a_lookup_tool,
    an_answer_of_two_that_is_playing,
    conversation_of,
    finish,
    interjection_while_playing,
    piece,
    the_hold_ends,
    the_person_finishes,
)

place = voice_support.place


def test_a_newer_talk_over_with_words_during_the_decision_makes_its_choice_stale_and_gets_its_own(
    place,
):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [
            lambda: trace.until(lambda: ("newer",) in trace.entries),
            piece("[continue]"),
            finish(),
        ],
        [piece("[silent]"), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield speaking(("Cy", "Noch eine Frage"))
        trace.record(("newer",))
        await trace.until(lambda: HoldEnded(HoldEnding.STOPPED) in trace.entries)
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        Woke(),
        StayedSilent(),
        HoldEnded(HoldEnding.STOPPED),
        DecisionEnded(),
    ]
    assert [call for call in trace.calls if call[0] == "pause"] == [("pause", "Eins.")]
    assert conversation_of(gateway.requests[2])[-1][1] == [
        "Moment, wie meinst du das?",
        {
            "held": HELD_AT_ONE["held"]
            | {"talked_over": [{"name": "Cy", "text": "Noch eine Frage"}]}
        },
    ]


def test_a_newer_talk_over_without_words_during_the_decision_delays_its_choice_until_it_ends(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [
            lambda: trace.until(lambda: ("newer",) in trace.entries),
            piece("[continue]"),
            finish(),
        ],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield speaking(("Cy", ""))
        trace.record(("newer",))
        await trace.settled()
        assert HoldEnded(HoldEnding.CONTINUED) not in trace.events
        assert [call for call in trace.calls if call[0] == "resume"] == []
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]
    assert [call for call in trace.calls if call[0] == "pause"] == [("pause", "Eins.")]


def test_a_second_hold_decision_waits_while_the_first_one_is_still_being_written(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [piece("[continue]"), a_look_up_call(), finish("tool_calls")],
        [finish()],
        [piece("[silent]"), finish()],
    )
    tools = [a_lookup_tool(trace, waits_for=lambda: ("written to its end",) in trace.entries)]

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        yield speaking(("Cy", "Noch eine Frage"))
        await trace.until(lambda: trace.count(Held()) == 2)
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()
        assert len(gateway.requests) == 2
        trace.record(("written to its end",))
        await trace.settled()
        assert len(gateway.requests) == 4

    assert place.run(hearing, gateway, voice=voice, tools=tools) == RETURNED

    assert HoldEnded(HoldEnding.STOPPED) in trace.events


@pytest.mark.parametrize("more_words_after_the_ending", [False, True])
def test_a_newer_talk_over_without_words_after_a_stopping_ending_holds_nothing_and_its_line_wakes(
    place, more_words_after_the_ending
):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [
            lambda: trace.until(lambda: ("newer",) in trace.entries),
            piece("[silent]"),
            finish(),
        ],
        [piece("Ja?"), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield speaking(("Cy", ""))
        trace.record(("newer",))
        await trace.until(lambda: HoldEnded(HoldEnding.STOPPED) in trace.entries)
        if more_words_after_the_ending:
            await trace.settled()
            yield speaking(("Cy", "Noch eine Frage, bitte"))
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.events.count(Held()) == 1
    assert trace.events[-5:] == [
        HoldEnded(HoldEnding.STOPPED),
        DecisionEnded(),
        Woke(),
        SentenceStarted("Ja?"),
        DecisionEnded(),
    ]


def test_an_utterance_that_begins_only_after_the_answer_ended_holds_nothing(place):
    voice = RecordingVoice(place.trace, held={"Eins."})
    gateway = FakeGateway(completion("Eins."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        voice.release("Eins.")
        await trace.settled()
        yield speaking(("Ben", "Moment"))
        yield a_line(2, "Moment.", name="Ben")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        DecisionEnded(),
        Woke(),
        SentenceStarted("Gern."),
        DecisionEnded(),
    ]
    assert len(gateway.requests) == 2


@pytest.mark.parametrize(
    ("reply", "unsaid"),
    [
        pytest.param("[continue]", ["Zwei."], id="continue-leaves-the-waiting-sentences-unsaid"),
        pytest.param("[finish]", [], id="finish-drops-the-waiting-sentences"),
    ],
)
def test_a_talk_over_that_starts_while_an_ending_resumes_the_sound_holds_it_again(
    place, reply, unsaid
):
    voice = RecordingVoice(place.trace, held={"Eins."}, seconds_per_resume=0.5)
    gateway = FakeGateway(THE_FIRST_ANSWER, completion(reply), completion("[silent]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: ("resume", "Eins.") in trace.entries)
        yield speaking(("Cy", "Noch eine Frage"))
        await trace.until(lambda: trace.events.count(Held()) == 2)
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events.count(Held()) == 2
    assert [call for call in place.trace.calls if call[0] == "pause"] == [("pause", "Eins.")] * 2
    assert ("play", "Zwei.") not in place.trace.calls
    assert conversation_of(gateway.requests[2])[-1][1][-1]["held"]["unsaid"] == unsaid


def test_a_resume_refused_ends_the_answer_as_a_refused_play_does(place):
    voice = RecordingVoice(
        place.trace, held={"Eins."}, raises={("resume", "Eins."): VoiceRefused()}
    )
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events[-3:] == [
        HoldEnded(HoldEnding.CONTINUED),
        SentenceRefused("Eins."),
        DecisionEnded(),
    ]
    assert ("stopped", "Eins.") in place.trace.calls
    assert ("play", "Zwei.") not in place.trace.calls


@pytest.mark.parametrize(
    ("hold_reply", "said_after_the_hold"),
    [
        ("[continue]", "Zwei."),
        ("Klar, ich höre zu.", "Klar, ich höre zu."),
        ("Klar. Ich höre zu.", "Klar. Ich höre zu."),
    ],
)
def test_the_next_decision_sees_what_was_said_before_and_after_the_hold(
    place, hold_reply, said_after_the_hold
):
    voice, gateway = an_answer_of_two_that_is_playing(
        place, completion(hold_reply), completion("Gern.")
    )

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event
        yield a_line(3, "Danke.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert conversation_of(gateway.requests[2]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins."),
        ("user", ["Moment, wie meinst du das?", HELD_AT_ONE]),
        ("assistant", said_after_the_hold),
        ("user", ["Danke."]),
    ]


@pytest.mark.parametrize("marker", ["[continue] Weiter.", "[finish]"])
def test_a_hold_marker_outside_a_hold_reads_unreadable_and_speaks_nothing(place, marker):
    gateway = FakeGateway(completion(marker))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert place.trace.events == [Woke(), ReplyUnreadable(), DecisionEnded()]
    assert place.trace.calls == []


@pytest.mark.parametrize(
    ("reply", "decision_events", "ending_events"),
    [
        ("[continue]", [], [HoldEnded(HoldEnding.CONTINUED), SentenceStarted("Zwei.")]),
        ("[finish]", [], [HoldEnded(HoldEnding.FINISHED)]),
        (
            "[continue] Also.",
            [],
            [
                HoldEnded(HoldEnding.SAID_THEN_CONTINUED),
                SentenceStarted("Also."),
                SentenceStarted("Zwei."),
            ],
        ),
        ("[silent]", [StayedSilent()], [HoldEnded(HoldEnding.STOPPED)]),
        ("Gut.", [], [HoldEnded(HoldEnding.ANSWERED), SentenceStarted("Gut.")]),
        ("[nein]", [ReplyUnreadable()], [HoldEnded(HoldEnding.DROPPED)]),
    ],
)
def test_a_hold_reports_woke_held_woke_and_its_ending_in_this_order(
    place, reply, decision_events, ending_events
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion(reply))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        *decision_events,
        *ending_events,
        DecisionEnded(),
    ]


def test_after_continue_the_next_waking_decision_starts_only_once_the_held_rest_has_played(place):
    voice = RecordingVoice(place.trace, held={"Eins.", "Zwei."})
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"), completion("Gern."))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.until(lambda: ("play", "Zwei.") in trace.entries)
        yield a_line(3, "Und dann?", name="Cy")
        await trace.settled()
        assert len(gateway.requests) == 2
        voice.release("Zwei.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert len(gateway.requests) == 3
    assert place.trace.played == ["Eins.", "Zwei.", "Gern."]
