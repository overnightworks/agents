"""A talk-over holds the voice loop's answer, and one decision ends the hold."""

import asyncio

import httpx2
import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, completion, tool_call, tool_round
from agents.backend.gateway import GatewayDetail
from agents.turn import FailureCode
from agents.voice import (
    DecisionEnded,
    DecisionFailed,
    Held,
    HoldEnded,
    HoldEnding,
    ReplyUnreadable,
    SentenceStarted,
    StayedSilent,
    Woke,
)
from tests.support import voice_support
from tests.support.voice_lines import a_line, speaking
from tests.support.voice_support import (
    A_GATEWAY_REFUSAL,
    HELD_AT_ONE,
    RETURNED,
    THE_FIRST_ANSWER,
    PacedGateway,
    RecordingVoice,
    a_lookup_tool,
    a_second_person_talks_over_beside_one_who_began_before_the_answer,
    an_answer_of_two_that_is_playing,
    conversation_of,
    finish,
    interjection_while_playing,
    messages_of,
    piece,
    rows_of,
    run_a_talk_over_before_the_first_sentence_is_complete,
    the_hold_ends,
    the_hold_ends_and_its_tools_settle,
    the_person_finishes,
)

place = voice_support.place


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
def test_a_partial_without_words_of_an_utterance_that_began_during_the_answer_holds_it(place, text):
    voice, gateway = an_answer_of_two_that_is_playing(place)

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("play", "Eins.") in trace.entries)
        yield speaking(("Ben", text))
        voice.release("Eins.")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held()]
    assert ("pause", "Eins.") in place.trace.calls
    assert ("play", "Zwei.") not in place.trace.calls


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
        async for event in a_second_person_talks_over_beside_one_who_began_before_the_answer(
            trace, words="Halt"
        ):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("pause", "Eins.") in place.trace.calls
    assert place.trace.events == [Woke(), SentenceStarted("Eins."), Held()]


def test_a_talk_over_while_the_reply_is_still_written_starts_no_sentence_until_the_ending(place):
    trace = place.trace
    run_a_talk_over_before_the_first_sentence_is_complete(place, "[continue]")

    assert trace.events == [
        Woke(),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.CONTINUED),
        SentenceStarted("Eins."),
        SentenceStarted("Zwei."),
        DecisionEnded(),
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
        ("user", ["Moment mal.", "Warte bitte.", HELD_AT_ONE]),
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

    assert rows_of(messages_of(gateway.requests[1])[-1])[-1] == HELD_AT_ONE
    assert trace.played == ["Eins.", "Zwei."]


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


def test_continue_with_words_says_them_instead_of_the_held_rest_then_the_rest_of_the_answer(
    place,
):
    voice, gateway = an_answer_of_two_that_is_playing(
        place, completion("[continue] Also, wie gesagt."), completion("Gern.")
    )

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event
        yield a_line(3, "Und jetzt?")
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    trace = place.trace
    assert trace.events[:8] == [
        Woke(),
        SentenceStarted("Eins."),
        Held(),
        Woke(),
        HoldEnded(HoldEnding.SAID_THEN_CONTINUED),
        SentenceStarted("Also, wie gesagt."),
        SentenceStarted("Zwei."),
        DecisionEnded(),
    ]
    assert ("stopped", "Eins.") in trace.calls
    assert ("resume", "Eins.") not in trace.calls
    assert trace.played == ["Also, wie gesagt.", "Zwei.", "Gern."]
    assert conversation_of(gateway.requests[2]) == [
        ("user", ["Erzähl."]),
        ("assistant", "Eins."),
        ("user", ["Moment, wie meinst du das?", HELD_AT_ONE]),
        ("assistant", "Also, wie gesagt. Zwei."),
        ("user", ["Und jetzt?"]),
    ]


def test_continue_with_words_still_being_written_when_chosen_plays_them_before_the_rest(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [
            piece("[continue] Also, wie gesagt. Noch"),
            lambda: trace.until(lambda: HoldEnded(HoldEnding.SAID_THEN_CONTINUED) in trace.entries),
            piece(" ein Satz."),
            finish(),
        ],
    )

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.played == ["Also, wie gesagt.", "Noch ein Satz.", "Zwei."]


def test_a_bare_continue_whose_turn_fails_before_its_round_ends_goes_on(place):
    trace = place.trace
    voice = RecordingVoice(trace, held={"Eins."})
    gateway = PacedGateway(
        trace,
        [piece("Eins. Zwei."), finish()],
        [piece("[continue]"), httpx2.ReadError("cut")],
    )

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert HoldEnded(HoldEnding.CONTINUED) in trace.events
    assert any(isinstance(event, DecisionFailed) for event in trace.events)
    assert HoldEnded(HoldEnding.DROPPED) not in trace.events
    assert trace.played == ["Eins.", "Zwei."]


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


@pytest.mark.parametrize(
    "hangs",
    [
        pytest.param((), id="the-next-render-returns"),
        pytest.param({("render", "Zwei.")}, id="the-next-render-hangs"),
    ],
)
def test_finish_resumes_the_paused_sentence_and_ends_the_answer_without_the_rest(place, hangs):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[finish]"), hangs=hangs)

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert place.trace.played == ["Eins."]
    assert ("resume", "Eins.") in place.trace.calls
    assert ("play", "Zwei.") not in place.trace.calls
    assert place.trace.events[-2:] == [HoldEnded(HoldEnding.FINISHED), DecisionEnded()]


@pytest.mark.parametrize(
    ("decision", "played"),
    [
        pytest.param("Klar. Gern.", ["Eins.", "Klar.", "Gern."], id="speech-replaces-the-rest"),
        pytest.param("[silent]", ["Eins."], id="silent-ends-the-answer"),
        pytest.param("[shout] Hallo.", ["Eins."], id="an-unreadable-decision-ends-the-answer"),
    ],
)
def test_a_hold_between_sentences_never_plays_the_sentence_rendered_ahead(place, decision, played):
    trace = place.trace
    voice = RecordingVoice(trace, seconds_per_render=3.0)
    gateway = FakeGateway(THE_FIRST_ANSWER, completion(decision))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("played", "Eins.") in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        assert ("render", "Zwei.") in trace.entries
        await trace.until(lambda: ("rendered", "Zwei.") in trace.entries)
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: trace.events[-1] == DecisionEnded())
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.played == played
    assert ("play", "Zwei.") not in trace.calls
    assert trace.events[-1] == DecisionEnded()


def test_an_answer_between_sentences_stops_the_held_turns_render_ahead_once(place):
    trace = place.trace
    voice = RecordingVoice(trace, seconds_per_render=3.0)
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("Klar."))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("played", "Eins.") in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: ("render", "Zwei.") in trace.entries)
        async for event in the_person_finishes(trace):
            yield event
        await trace.until(lambda: trace.events[-1] == DecisionEnded())
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.count(("unrendered", "Zwei.")) == 1
    assert ("rendered", "Zwei.") not in trace.entries
    assert trace.played == ["Eins.", "Klar."]


@pytest.mark.parametrize(
    ("rendering", "played", "unsaid"),
    [
        pytest.param("Eins.", [], ["Eins.", "Zwei."], id="before-the-first-sentence-played"),
        pytest.param("Zwei.", ["Eins."], ["Zwei."], id="after-a-sentence-played"),
    ],
)
def test_a_talk_over_while_the_next_sentence_renders_starts_no_sentence_and_tells_it_unsaid(
    place, rendering, played, unsaid
):
    trace = place.trace
    voice = RecordingVoice(trace, hangs={("render", rendering)})
    gateway = FakeGateway(THE_FIRST_ANSWER, completion("[silent]"))

    async def hearing(trace):
        yield a_line(1, "Erzähl.")
        await trace.until(lambda: ("render", rendering) in trace.entries)
        yield speaking(("Ben", "Moment"))
        await trace.until(lambda: Held() in trace.entries)
        async for event in the_person_finishes(trace):
            yield event
        await trace.settled()

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert trace.played == played
    assert all(call[0] not in {"pause", "resume"} for call in trace.calls)
    assert ("play", rendering) not in trace.calls
    assert conversation_of(gateway.requests[1])[-1][1][-1] == {"held": {"unsaid": unsaid}}


def test_silent_stops_the_paused_sentence_and_says_nothing_more(place):
    voice, gateway = an_answer_of_two_that_is_playing(place, completion("[silent]"))

    async def hearing(trace):
        async for event in the_hold_ends(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice) == RETURNED

    assert ("stopped", "Eins.") in place.trace.calls
    assert place.trace.played == []
    assert place.trace.events[-3:] == [
        StayedSilent(),
        HoldEnded(HoldEnding.STOPPED),
        DecisionEnded(),
    ]


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
    ("opening", "played"),
    [
        pytest.param("[continue] Hallo.", ["Hallo.", "Zwei."], id="a-continue-with-words"),
        pytest.param("[continue]", ["Eins.", "Zwei."], id="a-bare-continue"),
        pytest.param("[finish] Hallo.", ["Eins."], id="a-finish-with-words"),
        pytest.param("[silent] Hallo.", [], id="a-silent-with-words"),
    ],
)
def test_a_round_after_the_one_whose_opening_was_read_says_nothing_in_a_hold_decision(
    place, opening, played
):
    voice, gateway = an_answer_of_two_that_is_playing(
        place,
        tool_round(tool_call("call-1", "look_up", {}), content=opening),
        completion("Gefunden."),
    )
    tools = [a_lookup_tool(place.trace)]

    async def hearing(trace):
        async for event in the_hold_ends_and_its_tools_settle(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice, tools=tools) == RETURNED

    assert place.trace.played == played


@pytest.mark.parametrize("marker", ["[continue]", "[finish]", "[silent]"])
def test_the_tools_of_a_hold_decision_run_to_their_end_after_its_marker_ended_the_hold(
    place, marker
):
    voice, gateway = an_answer_of_two_that_is_playing(
        place, tool_round(tool_call("call-1", "look_up", {}), content=marker), completion()
    )
    tools = [a_lookup_tool(place.trace, takes_a_moment=True)]

    async def hearing(trace):
        async for event in the_hold_ends_and_its_tools_settle(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice, tools=tools) == RETURNED

    assert place.trace.count(("tool", "answered")) == 1


@pytest.mark.parametrize("marker", ["[continue]", "[finish]", "[silent]"])
def test_the_tools_of_a_hold_decision_finish_after_the_held_turn_failed_following_a_sentence(
    place, marker
):
    voice = RecordingVoice(place.trace, held={"Eins."})
    gateway = FakeGateway(
        tool_round(tool_call("call-1", "look_up", {}), content="Eins. Zwei."),
        A_GATEWAY_REFUSAL,
        tool_round(tool_call("call-2", "look_up", {}), content=marker),
        completion(),
    )
    tools = [a_lookup_tool(place.trace, takes_a_moment=True)]

    async def hearing(trace):
        async for event in the_hold_ends_and_its_tools_settle(trace, voice):
            yield event

    assert place.run(hearing, gateway, voice=voice, tools=tools) == RETURNED

    assert place.trace.count(("tool", "answered")) == 2


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
        (
            completion(""),
            [
                DecisionFailed(
                    TurnStopKind.FAILED,
                    FailureCode.PROVIDER_ERROR,
                    GatewayDetail.MALFORMED_REPLY,
                )
            ],
        ),
    ],
)
def test_a_failed_unreadable_or_empty_hold_decision_drops_the_rest_and_says_nothing(
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
        DecisionEnded(),
        Woke(),
        SentenceStarted("Gern."),
        DecisionEnded(),
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
