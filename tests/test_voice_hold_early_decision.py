"""The voice loop decides a hold from its first word on, told what people are still saying, and a
choice that plays waits for the talk-over's end; a choice told too few of their words is decided
again."""

from agents.backend.fake_gateway import completion
from agents.voice import (
    DecisionEnded,
    Held,
    HoldEnded,
    HoldEnding,
    SentenceStarted,
    StayedSilent,
    Woke,
)
from tests.support import voice_support
from tests.support.voice_lines import a_line, speaking
from tests.support.voice_support import (
    HELD_AT_ONE,
    RETURNED,
    PacedGateway,
    RecordingVoice,
    an_answer_of_two_that_is_playing,
    conversation_of,
    finish,
    interjection_while_playing,
    piece,
)

place = voice_support.place

HELD = HELD_AT_ONE["held"]


def held_row_telling(*talked_over: tuple[str, str]) -> dict:
    """The row of a hold at ``Eins.`` that tells what people are still saying."""
    told = [{"name": name, "text": text} for name, text in talked_over]
    return {"held": HELD | {"talked_over": told}}


def last_user_message(gateway, index: int) -> list:
    return conversation_of(gateway.requests[index])[-1][1]


def after(trace, mark: object):
    """A reply part that waits until ``mark`` is in the trace."""
    return lambda: trace.until(lambda: mark in trace.entries)


def marks(trace, mark: object):
    """A reply part that puts ``mark`` in the trace once the request is answered."""

    async def record() -> None:
        trace.record(mark)

    return record


def test_a_hold_decision_opens_at_the_first_word_and_is_told_what_the_talk_over_says(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[continue]"))
    trace = place.trace

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment mal"):
            yield event
        await trace.settled()
        assert len(gateway.requests) == 2
        assert HoldEnded(HoldEnding.CONTINUED) not in trace.events
        yield a_line(2, "Moment mal.", name="Ben")
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert last_user_message(gateway, 1) == [held_row_telling(("Ben", "Moment mal"))]
    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]


def test_a_talk_over_line_that_repeats_what_the_early_decision_was_told_wakes_nothing(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [after(trace, ("decided",)), piece("[continue]"), finish()],
        [piece("Gern."), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment mal"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield a_line(2, "Moment mal.", name="Ben")
        yield speaking()
        trace.record(("decided",))
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()
        assert trace.count(DecisionEnded()) == 1
        yield a_line(3, "Und weiter?", name="Cy")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.count(Woke()) == 3
    assert [text for text, _ in trace.asked] == ["Erzähl.", "Und weiter?"]
    assert last_user_message(gateway, 2)[-2:] == ["Moment mal.", "Und weiter?"]


def test_a_told_partial_that_ends_with_no_line_does_not_silence_a_later_line_of_its_words(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [after(trace, ("decided",)), piece("[continue]"), finish()],
        [piece("Gern."), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment mal"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield speaking()
        trace.record(("decided",))
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()
        yield a_line(2, "Moment mal.", name="Ben")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert [text for text, _ in trace.asked] == ["Erzähl.", "Moment mal."]
    assert trace.played[-1] == "Gern."


def test_a_line_with_new_words_before_the_choice_makes_it_stale_and_the_next_decision_is_told_it(
    place,
):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [after(trace, ("decided",)), piece("[continue]"), finish()],
        [piece("[continue]"), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield a_line(2, "Moment, wie meinst du das?", name="Ben")
        yield speaking()
        trace.record(("decided",))
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]
    assert last_user_message(gateway, 2) == ["Moment, wie meinst du das?", HELD_AT_ONE]


def test_a_stale_choice_plays_nothing_and_the_next_decision_opens_once_its_turn_closed(
    place,
):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [after(trace, ("decided",)), piece("[continue]"), finish()],
        [marks(trace, ("second request answered",)), piece("[continue]"), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield a_line(2, "Moment, wie meinst du das?", name="Ben")
        yield speaking()
        trace.record(("decided",))
        await trace.until(lambda: ("second request answered",) in trace.entries)
        assert HoldEnded(HoldEnding.CONTINUED) not in trace.events
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    closed, answered = ("closed",), ("second request answered",)
    assert trace.order_of(closed, answered)[:3] == [closed, closed, answered]
    assert conversation_of(gateway.requests[2])[:-1] == conversation_of(gateway.requests[1])[:-1]
    assert last_user_message(gateway, 2)[-1] == HELD_AT_ONE


def test_silent_over_words_its_decision_was_not_told_is_decided_again(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [after(trace, ("decided",)), piece("[silent]"), finish()],
        [piece("Ja?"), finish()],
    )

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield a_line(2, "Moment, wie meinst du das?", name="Ben")
        yield speaking()
        trace.record(("decided",))
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    assert HoldEnded(HoldEnding.STOPPED) not in trace.events
    assert HoldEnded(HoldEnding.ANSWERED) in trace.events
    assert trace.played == ["Ja?"]
    assert StayedSilent() in trace.events


def test_a_first_partial_after_the_choice_voids_it_and_a_wordless_new_hold_goes_on_by_itself(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [after(trace, ("decided",)), piece("[continue]"), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", "Moment mal"), ("Dan", ""))
        await trace.until(lambda: trace.count(Woke()) == 2)
        yield speaking(("Dan", ""))
        trace.record(("decided",))
        await trace.settled()
        assert HoldEnded(HoldEnding.CONTINUED) not in trace.events
        assert not any(call[0] == "resume" for call in trace.calls)
        yield speaking(("Dan", ""), ("Cy", ""))
        await trace.until(lambda: trace.count(Held()) == 2)
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.WENT_ON) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    assert trace.events[:6] == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        Held(),
        HoldEnded(HoldEnding.WENT_ON),
    ]
    assert [call for call in trace.calls if call[0] == "pause"] == [("pause", "Eins.")]


def test_new_words_while_a_choice_waits_void_it_and_the_hold_is_decided_again_at_the_same_place(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(
        place, completion("[continue]"), completion("[continue]")
    )
    trace = place.trace

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment mal"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        await trace.settled()
        yield speaking(("Ben", "Moment mal, warte"))
        await trace.until(lambda: trace.count(Woke()) == 3)
        await trace.settled()
        assert HoldEnded(HoldEnding.CONTINUED) not in trace.events
        yield a_line(2, "Moment mal, warte.", name="Ben")
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
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]
    assert conversation_of(gateway.requests[2])[:-1] == conversation_of(gateway.requests[1])[:-1]
    assert last_user_message(gateway, 2) == [held_row_telling(("Ben", "Moment mal, warte"))]
    assert [call for call in trace.calls if call[0] == "pause"] == [("pause", "Eins.")]


def test_a_choice_plays_in_the_step_of_the_snapshot_that_ends_the_talk_over_before_a_fresh_voice_holds(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(
        place, completion("[continue]"), completion("[continue]")
    )
    trace = place.trace

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment mal"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        await trace.settled()
        yield speaking()
        yield speaking(("Cy", "Eine Frage"))
        await trace.settled()
        yield speaking()
        await trace.settled()
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice, waking={"Erzähl."}) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]
    assert len(gateway.requests) == 3


def test_a_shorter_voice_while_a_choice_waits_reaches_the_next_decision_only_as_its_line(place):
    voice, gateway = an_answer_of_two_that_is_playing(
        place, completion("[continue]"), completion("Gut.")
    )
    trace = place.trace

    async def hearing(trace):
        async for event in interjection_while_playing(trace, words="Moment mal"):
            yield event
        await trace.until(lambda: trace.count(Woke()) == 2)
        await trace.settled()
        yield a_line(2, "Moment mal, ich meine etwas anderes.", name="Ben")
        yield speaking()
        await trace.until(lambda: HoldEnded(HoldEnding.CONTINUED) in trace.entries)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.events == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Zwei."),
        DecisionEnded(),
        Woke(),
        SentenceStarted("Gut."),
        DecisionEnded(),
    ]
    assert last_user_message(gateway, 2)[-1] == "Moment mal, ich meine etwas anderes."
