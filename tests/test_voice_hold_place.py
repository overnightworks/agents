"""A hold stops the sound and tells the agent where; hearing goes on while it stops."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.backend.fake_gateway import FakeGateway, completion
from agents.member import HoldPlace, HoldPoint
from agents.voice import Held, HoldEnded, HoldEnding, Woke
from agents.voice.conversation import told_hold
from tests.support import voice_support
from tests.support.voice_lines import a_line, speaking
from tests.support.voice_support import (
    RETURNED,
    THE_FIRST_ANSWER,
    PacedGateway,
    RecordingVoice,
    finish,
    interjection_while_playing,
    messages_of,
    piece,
    rows_of,
    the_hold_ends,
    the_person_finishes,
)

place = voice_support.place

NONE_HEARD = {"heard": "none", "rest": "Eins."}
A_WORD_HEARD = {"heard": "part", "said": "Eins", "rest": "."}
CANNOT_TELL = {"heard": "unknown", "sentence": "Eins."}
BETWEEN = {}


@pytest.mark.parametrize(
    ("at", "sentence", "heard_to", "told"),
    [
        pytest.param(
            HoldPoint.IN_SENTENCE,
            "Guten Tag, Ada.",
            11,
            {"heard": "part", "said": "Guten Tag, ", "rest": "Ada."},
            id="part of the sentence heard",
        ),
        pytest.param(
            HoldPoint.IN_SENTENCE,
            "Hallo \U0001f600 Ada.",
            6,
            {"heard": "part", "said": "Hallo ", "rest": "\U0001f600 Ada."},
            id="a part counted in code points",
        ),
        pytest.param(
            HoldPoint.IN_SENTENCE,
            "Guten Tag.",
            0,
            {"heard": "none", "rest": "Guten Tag."},
            id="nothing heard",
        ),
        pytest.param(
            HoldPoint.IN_SENTENCE,
            "Guten Tag.",
            None,
            {"heard": "unknown", "sentence": "Guten Tag."},
            id="the app cannot tell",
        ),
        pytest.param(HoldPoint.BETWEEN_SENTENCES, None, None, {}, id="between sentences"),
    ],
)
@pytest.mark.parametrize("unsaid", [[], ["Eins.", "Zwei."]], ids=["none unsaid", "two unsaid"])
def test_a_hold_is_told_in_one_of_four_shapes_beside_the_sentences_not_started(
    at, sentence, heard_to, told, unsaid
):
    assert told_hold(HoldPlace(at=at, sentence=sentence, heard_to=heard_to), unsaid) == {
        **told,
        "unsaid": unsaid,
    }


def the_hold_line_of(gateway: FakeGateway) -> object:
    return rows_of(messages_of(gateway.requests[1])[-1])[-1]


@pytest.mark.parametrize(
    ("voice_options", "first_answer", "reply", "told", "played", "ending"),
    [
        pytest.param(
            {"heard_to": {"Eins.": 4}},
            THE_FIRST_ANSWER,
            "[continue]",
            A_WORD_HEARD | {"unsaid": ["Zwei."]},
            ["Eins.", "Zwei."],
            HoldEnding.CONTINUED,
            id="P1 mid-sentence, continue",
        ),
        pytest.param(
            {"heard_to": {"Eins.": 4}},
            THE_FIRST_ANSWER,
            "[finish]",
            A_WORD_HEARD | {"unsaid": ["Zwei."]},
            ["Eins."],
            HoldEnding.FINISHED,
            id="P1 mid-sentence, finish",
        ),
        pytest.param(
            {"heard_to": {"Eins.": 4}},
            THE_FIRST_ANSWER,
            "[silent]",
            A_WORD_HEARD | {"unsaid": ["Zwei."]},
            [],
            HoldEnding.STOPPED,
            id="P1 mid-sentence, silent",
        ),
        pytest.param(
            {"heard_to": {"Eins.": 4}},
            THE_FIRST_ANSWER,
            "Klar.",
            A_WORD_HEARD | {"unsaid": ["Zwei."]},
            ["Klar."],
            HoldEnding.ANSWERED,
            id="P1 mid-sentence, speech",
        ),
        pytest.param(
            {"ends_at_pause": {"Eins."}},
            THE_FIRST_ANSWER,
            "[continue]",
            BETWEEN | {"unsaid": ["Zwei."]},
            ["Eins.", "Zwei."],
            HoldEnding.CONTINUED,
            id="P2 under the bound with a queue, continue",
        ),
        pytest.param(
            {"ends_at_pause": {"Eins."}},
            THE_FIRST_ANSWER,
            "[finish]",
            BETWEEN | {"unsaid": ["Zwei."]},
            ["Eins."],
            HoldEnding.FINISHED,
            id="P2 under the bound with a queue, finish",
        ),
        pytest.param(
            {"ends_at_pause": {"Eins."}},
            completion("Eins."),
            "[continue]",
            BETWEEN | {"unsaid": []},
            ["Eins."],
            HoldEnding.CONTINUED,
            id="P3 under the bound with an empty queue, continue",
        ),
        pytest.param(
            {"heard_to": {"Eins.": 0}},
            THE_FIRST_ANSWER,
            "[continue]",
            NONE_HEARD | {"unsaid": ["Zwei."]},
            ["Eins.", "Zwei."],
            HoldEnding.CONTINUED,
            id="P4 nothing heard, continue",
        ),
        pytest.param(
            {},
            THE_FIRST_ANSWER,
            "[continue]",
            CANNOT_TELL | {"unsaid": ["Zwei."]},
            ["Eins.", "Zwei."],
            HoldEnding.CONTINUED,
            id="P5 no word times, continue",
        ),
    ],
)
def test_the_hold_decision_is_told_where_the_sentence_stopped_and_its_ending_is_carried_out(
    place, voice_options, first_answer, reply, told, played, ending
):
    voice = RecordingVoice(place.trace, held={"Eins."}, **voice_options)
    gateway = FakeGateway(first_answer, completion(reply))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert the_hold_line_of(gateway) == {"held": told}
    assert place.trace.played == played
    assert HoldEnded(ending) in place.trace.events


def test_while_the_sound_stops_lines_and_partials_enter_and_the_decision_opens_once_it_has(place):
    voice = RecordingVoice(place.trace, held={"Eins."}, seconds_per_pause=30.0)
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        yield speaking(("Ben", "Moment mal"))
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()
        assert ("Moment, wie meinst du das?", False) in trace.asked
        assert len(gateway.requests) == 1
        await trace.until(lambda: len(gateway.requests) == 2)
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert [call for call in place.trace.calls if call[0] == "pause"] == [("pause", "Eins.")]
    assert place.trace.order_of(("pause", "Eins."), HoldEnded(HoldEnding.CONTINUED)) == [
        ("pause", "Eins."),
        HoldEnded(HoldEnding.CONTINUED),
    ]
    assert place.trace.played == ["Eins.", "Zwei."]


def test_the_sound_is_paused_while_its_sentence_plays_even_as_the_play_is_about_to_return(place):
    voice = RecordingVoice(place.trace, held={"Eins."})
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        voice.release("Eins.")
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.order_of(("pause", "Eins."), ("played", "Eins."))[0] == ("pause", "Eins.")


def test_a_sound_that_never_stops_holds_the_decision_while_hearing_goes_on_until_it_ends(place):
    voice = RecordingVoice(place.trace, held={"Eins."}, hangs={("pause", "Eins.")})
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[continue]"))

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("Moment, wie meinst du das?", False) in place.trace.asked
    assert len(gateway.requests) == 1
    assert ("unpaused", "Eins.") in place.trace.calls
    assert ("stopped", "Eins.") in place.trace.calls


def test_a_stop_that_reports_more_than_the_sentence_was_long_fails_loud(place):
    voice = RecordingVoice(place.trace, held={"Eins."}, heard_to={"Eins.": 6})
    gateway = FakeGateway(THE_FIRST_ANSWER)

    async def hearing(trace):
        async for event in interjection_while_playing(trace):
            yield event
        await trace.settled()

    ending = place.run(hearing, gateway, voice=voice)

    assert ending.raised is not None
    assert ending.raised.subgroup(ValidationError) is not None
    assert ("stopped", "Eins.") in place.trace.calls


@pytest.mark.parametrize(
    ("reply", "told"),
    [
        pytest.param("[continue]", CANNOT_TELL | {"unsaid": ["Zwei."]}, id="continue"),
        pytest.param("[finish]", CANNOT_TELL | {"unsaid": []}, id="finish"),
        pytest.param("Klar.", BETWEEN | {"unsaid": ["Klar."]}, id="speech"),
    ],
)
def test_a_playing_ending_under_a_newer_talk_over_holds_again_told_where_the_sound_stands(
    place, reply, told
):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [lambda: trace.until(lambda: ("newer",) in trace.entries), piece(reply), finish()],
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
        yield a_line(3, "Noch eine Frage, bitte.", name="Cy")
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert rows_of(messages_of(gateway.requests[2])[-1])[-1] == {"held": told}
