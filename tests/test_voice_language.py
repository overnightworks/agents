"""The voice loop knows the language of each heard line and renders in the answered line's."""

import pytest

from agents.backend.fake_gateway import FakeGateway, completion
from agents.voice import Held, Woke
from tests.support import voice_support
from tests.support.voice_lines import a_line, speaking
from tests.support.voice_support import (
    RETURNED,
    THE_FIRST_ANSWER,
    PacedGateway,
    RecordingVoice,
    an_answer_of_two_that_is_playing,
    finish,
    messages_of,
    piece,
    rows_of,
    the_person_finishes,
)

place = voice_support.place


def rendered_in(place, sentence):
    return [language for said, language in place.trace.rendered_languages if said == sentence]


def a_second_answer_over(place, *lines):
    """Lines heard while a first answer plays wake one more decision; what it rendered in."""
    voice = RecordingVoice(place.trace, held={"Eins."})
    gateway = FakeGateway(completion("Eins."), completion("Gern."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        for line in lines:
            yield line
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED
    return rendered_in(place, "Gern.")


def a_talk_over_the_held_answer(first_language, interjection_language, voice):
    async def hearing(trace):
        yield a_line(1, "Erzähl.", language=first_language)
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        async for event in the_person_finishes(trace, language=interjection_language):
            yield event
        await trace.settled()
        voice.release("Eins.")
        await trace.settled()

    return hearing


@pytest.mark.parametrize("language", ["de", "en"])
def test_an_answer_renders_in_the_language_of_the_line_it_answers(place, language):
    async def hearing(trace):
        yield a_line(1, "Erzähl.", language=language)
        await trace.settled()

    assert place.run(hearing, FakeGateway(THE_FIRST_ANSWER)) == RETURNED

    assert place.trace.rendered_languages == [("Eins.", language), ("Zwei.", language)]


@pytest.mark.parametrize("spoken", [True, False])
def test_an_answer_to_a_line_that_names_no_language_renders_with_none(place, spoken):
    async def hearing(trace):
        yield a_line(1, "Erzähl.", spoken=spoken)
        await trace.settled()

    assert place.run(hearing, FakeGateway(THE_FIRST_ANSWER)) == RETURNED

    assert place.trace.rendered_languages == [("Eins.", None), ("Zwei.", None)]


@pytest.mark.parametrize(("newer", "expected"), [(None, None), ("en", "en")])
def test_an_answer_takes_the_answered_lines_language_never_an_older_lines(place, newer, expected):
    lines = [a_line(2, "Und dann?", language="de"), a_line(3, "Weiter.", language=newer)]

    assert a_second_answer_over(place, *lines) == [expected]


def an_own_and_an_act_line_after_a_person_line():
    return [
        a_line(2, "Und dann?", language="de"),
        a_line(3, "Ich schreibe.", name="Ich", own=True, spoken=False),
        a_line(4, "Ada legt ab.", kind="move", spoken=False, act=True),
    ]


def test_an_own_or_act_line_after_the_answered_line_keeps_its_language(place):
    assert a_second_answer_over(place, *an_own_and_an_act_line_after_a_person_line()) == ["de"]


def test_a_window_drop_that_leaves_no_line_with_a_language_renders_with_none(place):
    place.window_bytes = 1

    assert a_second_answer_over(place, *an_own_and_an_act_line_after_a_person_line()) == [None]


@pytest.mark.parametrize(("interjection", "expected"), [("en", "en"), (None, None)])
def test_an_answer_that_replaces_a_held_one_renders_in_the_language_of_the_interjection_it_answers(
    place, interjection, expected
):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("Klar, ich höre zu."))

    assert (
        place.run(a_talk_over_the_held_answer("de", interjection, voice), gateway, voice=voice)
        == RETURNED
    )

    assert rendered_in(place, "Klar, ich höre zu.") == [expected]


@pytest.mark.parametrize("marker", ["[continue]", "[finish]"])
@pytest.mark.parametrize("interjection", ["en", None])
def test_a_held_answer_that_goes_on_renders_only_in_its_own_language(place, marker, interjection):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion(marker))

    assert (
        place.run(a_talk_over_the_held_answer("de", interjection, voice), gateway, voice=voice)
        == RETURNED
    )

    assert {language for _, language in place.trace.rendered_languages} == {"de"}


def test_a_replacing_answer_over_no_new_line_renders_with_none_once_a_sentence_started(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("Klar, ich höre zu."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.", language="de")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        yield speaking()
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert rendered_in(place, "Klar, ich höre zu.") == [None]


def test_a_replacing_answer_over_no_new_line_before_a_sentence_started_renders_in_the_waking_lines(
    place,
):
    trace = place.trace
    gateway = PacedGateway(
        trace,
        [
            piece("Ein"),
            lambda: trace.until(lambda: Held() in trace.entries),
            piece("s. Zwei."),
            finish(),
        ],
        [piece("Klar, ich höre zu."), finish()],
    )

    async def hearing(trace):
        yield a_line(1, "Erzähl.", language="de")
        await trace.until(lambda: Woke() in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        yield speaking()
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert rendered_in(place, "Klar, ich höre zu.") == ["de"]


def test_a_line_reaches_the_model_with_its_language_only_when_it_names_one(place):
    gateway = FakeGateway(completion("Gut."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.", language="de")
        await trace.settled()
        yield a_line(2, "Und?")
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    sent = [rows_of(messages_of(request)[-1]) for request in gateway.requests]
    assert sent[0][0]["language"] == "de"
    assert "language" not in sent[1][0]
