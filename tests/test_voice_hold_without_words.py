"""A talk-over through which no word was heard ends by itself, once nobody speaks."""

import asyncio

import pytest

from agents.backend.fake_gateway import FakeGateway, completion
from agents.voice import (
    DecisionEnded,
    Held,
    HoldEnded,
    HoldEnding,
    SentenceRefused,
    SentenceStarted,
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
    a_talk_over_without_words_beside_a_person_who_began_before_the_answer,
    an_answer_of_two_that_is_playing,
    conversation_of,
    finish,
    interjection_while_playing,
    piece,
)

place = voice_support.place

WENT_ON = HoldEnded(HoldEnding.WENT_ON)
THE_WENT_ON_LINE = {"went_on": True}
THE_ANSWER_AFTER_GOING_ON = [
    Woke(),
    SentenceStarted("Eins."),
    Held(),
    WENT_ON,
    SentenceStarted("Zwei."),
    DecisionEnded(),
]


def a_voice(place, **arrangement):
    return RecordingVoice(place.trace, held={"Eins."}, **arrangement)


async def the_sound_goes_on(trace, voice):
    """Wait for the hold to end by itself and free the sentence it held."""
    await trace.until(lambda: WENT_ON in trace.entries)
    voice.release("Eins.")
    await trace.settled()


@pytest.mark.parametrize(
    "arrangement",
    [
        pytest.param({"heard_to": {"Eins.": 2}}, id="in a sentence, heard in part"),
        pytest.param({"heard_to": {"Eins.": 0}}, id="in a sentence, no word heard"),
        pytest.param({}, id="in a sentence, the voice cannot tell"),
        pytest.param({"ends_at_pause": {"Eins."}}, id="the sentence ended as it paused"),
    ],
)
def test_a_talk_over_without_words_goes_on_by_itself_and_the_rest_plays_with_no_decision(
    place, arrangement
):
    voice = a_voice(place, **arrangement)
    gateway = FakeGateway(THE_FIRST_ANSWER)

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="…"):
            yield event
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == THE_ANSWER_AFTER_GOING_ON
    assert place.trace.played == ["Eins.", "Zwei."]
    assert len(gateway.requests) == 1
    assert (("resume", "Eins.") in place.trace.calls) is ("ends_at_pause" not in arrangement)


def test_a_talk_over_without_words_while_nothing_plays_goes_on_without_waiting_for_the_reading(
    place,
):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Ein"),
            lambda: trace.until(lambda: WENT_ON in trace.entries),
            piece("s. Zwei."),
            finish(),
        ],
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: Woke() in trace.entries)
        yield speaking(("Ben", ""))
        await trace.until(lambda: Held() in trace.entries)
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace.events == [
        Woke(),
        Held(),
        WENT_ON,
        SentenceStarted("Eins."),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]
    assert all(call[0] not in {"pause", "resume"} for call in trace.calls)
    assert len(gateway.requests) == 1


def test_a_talk_over_that_ends_with_no_word_while_the_pause_is_pending_goes_on_after_the_pause(
    place,
):
    voice = a_voice(place, seconds_per_pause=5.0)
    gateway = FakeGateway(THE_FIRST_ANSWER)

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking()
        await trace.settled()
        assert WENT_ON not in trace.entries
        await asyncio.sleep(10)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == THE_ANSWER_AFTER_GOING_ON
    assert len(gateway.requests) == 1


def test_the_next_waking_decision_is_told_that_the_agent_went_on(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("Gern."))

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking()
        await the_sound_goes_on(trace, voice)
        yield a_line(2, "Danke.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins. Zwei."),
        ("user", [THE_WENT_ON_LINE, "Danke."]),
    ]


def test_a_talk_over_of_two_people_goes_on_only_once_both_have_ended_without_a_word(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking(("Ben", ""), ("Cy", "…"))
        yield speaking(("Cy", "…"))
        await trace.settled()
        assert WENT_ON not in trace.entries
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == THE_ANSWER_AFTER_GOING_ON
    assert len(gateway.requests) == 1


def test_a_talk_over_of_two_people_one_of_whom_shows_a_word_gets_its_decision(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking(("Ben", ""), ("Cy", "Halt"))
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert WENT_ON not in place.trace.events
    assert len(gateway.requests) == 2


def test_a_worded_line_of_a_person_whose_partial_had_none_gets_the_decision(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield a_line(2, "Moment mal.", name="Ben")
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert WENT_ON not in place.trace.events
    assert conversation_of(gateway.requests[1])[-1] == ("user", ["Moment mal.", HELD_AT_ONE])


def test_a_hold_waits_for_a_person_who_began_before_the_answer_to_stop_and_then_goes_on(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        async for event in a_talk_over_without_words_beside_a_person_who_began_before_the_answer(
            trace
        ):
            yield event
        assert WENT_ON not in trace.entries
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == THE_ANSWER_AFTER_GOING_ON
    assert len(gateway.requests) == 1


def test_a_short_worded_line_of_a_person_of_the_talk_over_gets_a_decision_while_another_speaks(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in a_talk_over_without_words_beside_a_person_who_began_before_the_answer(
            trace
        ):
            yield event
        assert WENT_ON not in trace.entries
        yield a_line(2, "Stopp.", name="Cy")
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert conversation_of(gateway.requests[1])[-1] == ("user", ["Stopp.", HELD_AT_ONE])
    assert WENT_ON not in place.trace.events


def test_a_voice_that_starts_afresh_after_the_going_on_holds_the_agent_again(place):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking()
        await trace.until(lambda: WENT_ON in trace.entries)
        yield speaking(("Ben", ""))
        await trace.until(lambda: trace.count(Held()) == 2)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert [call for call in place.trace.calls if call[0] == "pause"] == [("pause", "Eins.")] * 2
    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held(), WENT_ON, Held()]


async def a_decision_that_ends_the_hold_while_a_newer_person_says_a_word(trace):
    async for event in interjection_while_playing(trace):
        yield event
    yield speaking()
    await trace.until(lambda: trace.count(Woke()) == 2)
    yield speaking(("Cy", "Halt"))
    yield speaking()
    trace.record(("newer",))
    await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
    await trace.settled()


async def a_going_on_and_a_short_line_of_the_talker_without_a_partial(trace):
    async for event in interjection_while_playing(trace, words=""):
        yield event
    yield speaking()
    await trace.until(lambda: WENT_ON in trace.entries)
    yield a_line(2, "Ja.", name="Ben")
    await trace.settled()


def the_decision_ends_once_a_newer_person_has_ended(trace):
    return PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [lambda: trace.until(lambda: ("newer",) in trace.entries), piece("[continue]"), finish()],
    )


THE_SECOND_TALK_OVER_GOES_ON = [Held(), WENT_ON, SentenceStarted("Zwei."), DecisionEnded()]


@pytest.mark.parametrize(
    ("first_hold", "make_gateway", "events_up_to_the_answer_end"),
    [
        pytest.param(
            a_decision_that_ends_the_hold_while_a_newer_person_says_a_word,
            the_decision_ends_once_a_newer_person_has_ended,
            [
                Woke(),
                SentenceStarted("Eins."),
                Held(),
                Woke(),
                HoldEnded(HoldEnding.CONTINUED),
                *THE_SECOND_TALK_OVER_GOES_ON,
            ],
            id="a newer person said a word during the decision",
        ),
        pytest.param(
            a_going_on_and_a_short_line_of_the_talker_without_a_partial,
            lambda trace: FakeGateway(THE_FIRST_ANSWER, completion("Gern.")),
            [Woke(), SentenceStarted("Eins."), Held(), WENT_ON, *THE_SECOND_TALK_OVER_GOES_ON],
            id="the talker of a hold that went on says a short line",
        ),
    ],
)
def test_a_second_talk_over_without_words_in_the_answer_goes_on_by_itself_after_a_first_hold(
    place, first_hold, make_gateway, events_up_to_the_answer_end
):
    voice = a_voice(place)

    async def hearing(trace):
        async for event in first_hold(trace):
            yield event
        yield speaking(("Dan", ""))
        await trace.until(lambda: trace.count(Held()) == 2)
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, make_gateway(place.trace), voice=voice) == RETURNED

    events = place.trace.events
    assert events[: events.index(DecisionEnded()) + 1] == events_up_to_the_answer_end


def test_a_talk_over_with_words_after_the_going_on_tells_the_decision_the_going_on_before_the_hold(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking()
        await trace.until(lambda: WENT_ON in trace.entries)
        yield speaking(("Cy", "Halt"))
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins."),
        ("user", [THE_WENT_ON_LINE, HELD_AT_ONE]),
    ]


@pytest.mark.parametrize(
    ("decision", "ending", "starts_after", "resumes_the_stopped_sound"),
    [
        pytest.param(
            "[continue]", HoldEnding.CONTINUED, [SentenceStarted("Zwei.")], True, id="continued"
        ),
        pytest.param("[finish]", HoldEnding.FINISHED, [], True, id="finished"),
        pytest.param("Gut.", HoldEnding.ANSWERED, [SentenceStarted("Gut.")], False, id="answered"),
    ],
)
def test_a_newer_talk_over_without_words_plays_what_the_waiting_ending_would_play(
    place, decision, ending, starts_after, resumes_the_stopped_sound
):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [lambda: trace.until(lambda: ("newer",) in trace.entries), piece(decision), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        yield a_line(2, "Moment mal.", name="Ben")
        yield speaking()
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield speaking(("Cy", ""))
        trace.record(("newer",))
        await trace.until(lambda: trace.count(Held()) == 2)
        assert WENT_ON not in trace.entries
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(ending),
        Held(),
        WENT_ON,
        *starts_after,
        DecisionEnded(),
    ]
    assert (("resume", "Eins.") in trace.calls) is resumes_the_stopped_sound
    assert len(gateway.requests) == 2


def test_a_resume_the_host_refuses_at_the_going_on_ends_the_answer(place):
    voice = RecordingVoice(
        place.trace, held={"Eins."}, raises={("resume", "Eins."): VoiceRefused()}
    )
    gateway = FakeGateway(THE_FIRST_ANSWER)

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events[-3:] == [WENT_ON, SentenceRefused("Eins."), DecisionEnded()]
    assert ("play", "Zwei.") not in place.trace.calls
    assert len(gateway.requests) == 1


def test_a_worded_line_of_a_person_who_never_had_a_partial_wakes_one_decision_after_the_answer(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("Gern."))

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=""):
            yield event
        yield a_line(2, "Stopp.", name="Cy")
        yield speaking()
        await the_sound_goes_on(trace, voice)

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events.count(WENT_ON) == 1
    assert len(gateway.requests) == 2
    assert conversation_of(gateway.requests[1]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins. Zwei."),
        ("user", ["Stopp.", THE_WENT_ON_LINE]),
    ]


@pytest.mark.parametrize(
    ("words", "reply", "ending"),
    [
        pytest.param("", None, WENT_ON, id="after a talk-over without words went on"),
        pytest.param(
            "Moment",
            completion("[continue]"),
            HoldEnded(HoldEnding.CONTINUED),
            id="after a continue",
        ),
    ],
)
def test_a_voice_that_comes_as_soon_as_a_hold_ended_holds_the_answer_and_starts_no_sentence(
    place, words, reply, ending
):
    voice = RecordingVoice(place.trace, held={"Eins."}, ends_at_pause={"Eins."})
    gateway = FakeGateway(THE_FIRST_ANSWER, *([reply] if reply else []))

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words=words):
            yield event
        yield speaking()
        await trace.until(lambda: ending in trace.entries)
        yield speaking(("Cy", "Noch"))
        await trace.until(lambda: trace.count(Held()) == 2)
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events[-1] == Held()
    assert ("play", "Zwei.") not in place.trace.calls
