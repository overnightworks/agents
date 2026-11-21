"""A talk-over holds the voice loop's answer, and one decision ends the hold."""

import asyncio

import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, completion
from agents.backend.gateway import GatewayDetail
from agents.turn import FailureCode
from agents.voice import (
    DecisionFailed,
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
    A_GATEWAY_REFUSAL,
    RETURNED,
    PacedGateway,
    RecordingVoice,
    conversation_of,
    finish,
    messages_of,
    piece,
    rows_of,
)

place = voice_support.place

PAUSED_AT_ONE = {"paused_at": "Eins.", "unsaid": "Zwei."}
THE_FIRST_ANSWER = completion("Eins. Zwei.")


def an_answer_of_two_that_is_playing(place, *later_replies):
    """A voice that plays ``Eins.`` until released and a gateway whose first reply has both."""
    voice = RecordingVoice(place.trace, held={"Eins."})
    return voice, FakeGateway(THE_FIRST_ANSWER, *later_replies)


async def interjection_while_playing(trace):
    """The first line wakes the agent; a person then starts talking over its playing sentence."""
    yield a_line(1, "Erzähl.")
    await trace.until(lambda: ("play", "Eins.") in trace.entries)
    yield speaking(("Ben", "Moment"))
    await trace.until(lambda: Held() in trace.entries)


async def the_person_finishes(trace, *, text="Moment, wie meinst du das?", seq=2):
    yield a_line(seq, text, name="Ben")
    yield speaking()


def test_the_first_words_of_a_talk_over_pause_the_playing_sentence_and_start_no_other(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        yield speaking(("Ben", "Moment mal"))
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.calls[:3] == [("render", "Eins."), ("play", "Eins."), ("render", "Zwei.")]
    assert ("pause", "Eins.") in place.trace.calls
    assert ("play", "Zwei.") not in place.trace.calls
    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held()]
    assert len(gateway.requests) == 1


@pytest.mark.parametrize("text", ["", "…", " ?! "])
def test_a_talk_over_without_words_never_pauses_the_answer(place, text):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", text))
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins.", "Zwei."]
    assert Held() not in place.trace.events
    assert ("pause", "Eins.") not in place.trace.calls


def test_an_utterance_that_began_before_the_decision_never_pauses_its_answer(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        yield speaking(("Ben", "Ich glaube"))
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", "Ich glaube, dass"))
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins.", "Zwei."]
    assert Held() not in place.trace.events


def test_a_second_person_who_starts_during_the_answer_holds_it_while_an_earlier_speaker_goes_on(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        yield speaking(("Ben", "Ich glaube"))
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", "Ich glaube"), ("Cy", "Halt"))
        await trace.until(lambda: Held() in trace.entries)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("pause", "Eins.") in place.trace.calls
    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held()]


def test_a_talk_over_while_the_reply_is_still_written_starts_no_sentence_until_the_ending(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Ein"),
            lambda: trace.until(lambda: Held() in trace.entries),
            piece("s. Zwei."),
            finish(),
        ],
        [piece("[continue]"), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: Woke() in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace.events == [
        Woke(),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Eins."),
        SentenceStarted("Zwei."),
    ]
    assert all(call[0] not in {"pause", "resume"} for call in trace.calls)


def test_the_hold_decision_starts_once_the_last_speaker_finished_and_sends_what_was_said_and_unsaid(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        yield speaking(("Ben", "Moment"), ("Cy", "Warte"))
        yield a_line(2, "Moment mal.", name="Ben")
        yield speaking(("Cy", "Warte"))
        await trace.settled()
        assert len(gateway.requests) == 1
        yield a_line(3, "Warte bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins."),
        ("user", ["Moment mal.", "Warte bitte.", PAUSED_AT_ONE]),
    ]


def test_a_persons_finished_line_ends_their_part_of_the_talk_over_before_their_partial_goes(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        yield a_line(2, "Moment mal.", name="Ben")
        await trace.settled()
        assert len(gateway.requests) == 2
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events.count(Woke()) == 2


def test_the_hold_decision_waits_for_the_held_reply_to_be_written_to_its_end(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Eins. Zw"),
            lambda: trace.until(lambda: ("tail",) in trace.entries),
            piece("ei."),
            finish(),
        ],
        [piece("[continue]"), finish()],
    )
    voice = RecordingVoice(trace, held={"Eins."})

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()
        assert len(gateway.requests) == 1
        trace.record(("tail",))
        await trace.until(lambda: len(gateway.requests) == 2)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert rows_of(messages_of(gateway.requests[1])[-1])[-1] == PAUSED_AT_ONE
    assert trace.played == ["Eins.", "Zwei."]


async def the_hold_ends(trace, voice):
    """A talk-over that ended: the person's line and then no partial; the held sound is freed."""
    async for event in interjection_while_playing(trace):
        yield event
    async for event in the_person_finishes(trace):
        yield event
    await trace.until(lambda: any(isinstance(entry, HoldEnded) for entry in trace.entries))
    voice.release("Eins.")
    await trace.settled()


def test_continue_resumes_the_paused_sentence_and_plays_the_rest_of_the_answer(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins.", "Zwei."]
    pauses = [call for call in place.trace.calls if call[0] in {"pause", "resume", "play"}]
    assert pauses == [
        ("play", "Eins."),
        ("pause", "Eins."),
        ("resume", "Eins."),
        ("play", "Zwei."),
    ]


def test_continue_after_a_pause_that_met_the_sounds_end_plays_the_rest_at_once(place):
    voice = RecordingVoice(place.trace, held={"Eins."}, ends_at_pause={"Eins."})
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins.", "Zwei."]


def test_finish_resumes_the_paused_sentence_and_drops_the_rest(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[finish]"))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins."]
    assert ("resume", "Eins.") in place.trace.calls
    assert ("play", "Zwei.") not in place.trace.calls
    assert place.trace.events[-1] == HoldEnded(HoldEnding.FINISHED)


def test_finish_with_no_sentence_paused_says_nothing_more(place):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Ein"),
            lambda: trace.until(lambda: Held() in trace.entries),
            piece("s. Zwei."),
            finish(),
        ],
        [piece("[finish]"), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: Woke() in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace.events == [Woke(), Held(), Woke(), HoldEnded(HoldEnding.FINISHED)]
    assert trace.played == []


def test_silent_stops_the_paused_sentence_and_says_nothing_more(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[silent]"))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("stopped", "Eins.") in place.trace.calls
    assert place.trace.played == []
    assert place.trace.events[-2:] == [StayedSilent(), HoldEnded(HoldEnding.STOPPED)]


def test_speech_in_the_hold_decision_replaces_the_rest_with_no_further_request(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("Klar, ich höre zu."))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Klar, ich höre zu."]
    assert ("stopped", "Eins.") in place.trace.calls
    assert ("play", "Zwei.") not in place.trace.calls
    assert len(gateway.requests) == 2


@pytest.mark.parametrize(
    ("hold_reply", "decision_events"),
    [
        (
            A_GATEWAY_REFUSAL,
            [
                DecisionFailed(
                    TurnStopKind.FAILED,
                    FailureCode.PROVIDER_UNAVAILABLE,
                    GatewayDetail.GATEWAY_REFUSED,
                )
            ],
        ),
        (completion("[shout] Hallo."), [ReplyUnreadable()]),
    ],
)
def test_a_hold_decision_that_fails_or_reads_unreadable_drops_the_rest_and_says_nothing(
    place, hold_reply, decision_events
):
    voice, gateway = an_answer_of_two_that_is_playing(place, hold_reply, completion("Gern."))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event
        yield a_line(3, "Und jetzt?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        *decision_events,
        HoldEnded(HoldEnding.DROPPED),
        Woke(),
        SentenceStarted("Gern."),
    ]
    assert ("stopped", "Eins.") in place.trace.calls
    assert place.trace.played == ["Gern."]
    assert conversation_of(gateway.requests[2]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins."),
        ("user", ["Moment, wie meinst du das?", "Und jetzt?"]),
    ]


def test_a_talk_over_whose_partials_go_without_a_line_still_gets_a_decision(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        yield speaking()
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert HoldEnded(HoldEnding.CONTINUED) in place.trace.events
    assert place.trace.played == ["Eins.", "Zwei."]


def test_a_hold_stands_while_any_person_of_the_talk_over_still_speaks_and_no_timer_ends_it(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        await asyncio.sleep(3600)
        yield speaking(("Ben", "Moment, ich meine"), ("Cy", "Halt"))
        await asyncio.sleep(3600)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert len(gateway.requests) == 1
    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held()]
    assert [call for call in place.trace.calls if call[0] == "resume"] == []


def test_when_hearing_ends_while_held_the_loop_stops_the_paused_sound_and_returns(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("stopped", "Eins.") in place.trace.calls
    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held()]


def test_an_ending_waits_while_a_newer_talk_over_stands_and_that_talk_over_gets_its_own_decision(
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
        await trace.until(lambda: trace.count(Held()) == 2)
        assert [call for call in trace.calls if call[0] == "resume"] == []
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        Held(),
        Woke(),
        StayedSilent(),
        HoldEnded(HoldEnding.STOPPED),
    ]
    assert [call for call in trace.calls if call[0] == "pause"] == [("pause", "Eins.")]
    assert conversation_of(gateway.requests[2])[-2:] == [
        ("assistant", "[continue]"),
        ("user", ["Noch eine Frage, bitte.", PAUSED_AT_ONE]),
    ]


def test_a_newer_talk_over_after_a_stopping_ending_holds_nothing_and_its_line_wakes_as_any_line(
    place,
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
        yield speaking(("Cy", "Noch eine Frage"))
        trace.record(("newer",))
        await trace.until(lambda: HoldEnded(HoldEnding.STOPPED) in trace.entries)
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.events.count(Held()) == 1
    assert trace.events[-3:] == [HoldEnded(HoldEnding.STOPPED), Woke(), SentenceStarted("Ja?")]


def test_a_resume_refused_ends_the_answer_as_a_refused_play_does(place):
    voice = RecordingVoice(
        place.trace, held={"Eins."}, raises={("resume", "Eins."): VoiceRefused()}
    )
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events[-2:] == [HoldEnded(HoldEnding.CONTINUED), SentenceRefused("Eins.")]
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
        ("user", ["Moment, wie meinst du das?", PAUSED_AT_ONE]),
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

    assert place.trace.events == [Woke(), ReplyUnreadable()]
    assert place.trace.calls == []


@pytest.mark.parametrize(
    ("reply", "decision_events", "ending_events"),
    [
        ("[continue]", [], [HoldEnded(HoldEnding.CONTINUED), SentenceStarted("Zwei.")]),
        ("[finish]", [], [HoldEnded(HoldEnding.FINISHED)]),
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
