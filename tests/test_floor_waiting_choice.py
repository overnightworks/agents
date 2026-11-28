"""One floor rule: a choice that plays starts only once every person of the hold's talk-over has
ended their turn, a choice told too few of their words is refused, and a voice that goes on after
the choice ends the hold. Every case drives `agents.floor` through its events."""

from collections.abc import Callable

import pytest

from agents.floor import (
    Drop,
    Floor,
    HoldChoice,
    HoldEnding,
    PlayOn,
    Stale,
    ToldTalkOver,
    Wait,
)
from agents.member_speaking import (
    ContinueSpeakingRefusal,
    PartialSpeech,
    SpeakRefusal,
    StopSpeakingRefusal,
)
from tests.support.floor_support import (
    FIRST,
    HOLD,
    SECOND,
    held_while_speaking,
    lasts,
    new_floor,
    partial,
    place_in,
    playing,
)

REPLY = "Ja?"
CUT = "Eins"

PLAYING_CHOICES = [
    pytest.param(HoldEnding.CONTINUED, None, (PlayOn(FIRST),), (SECOND,), id="continue"),
    pytest.param(
        HoldEnding.FINISHED, None, (Drop(None, (SECOND,), None), PlayOn(FIRST)), (), id="finish"
    ),
    pytest.param(
        HoldEnding.ANSWERED,
        REPLY,
        (Drop(FIRST, (SECOND,), CUT),),
        (REPLY,),
        id="a speak naming the hold",
    ),
    pytest.param(
        HoldEnding.SAID_THEN_CONTINUED,
        REPLY,
        (Drop(FIRST, (), CUT),),
        (REPLY,),
        id="a short line",
    ),
]


def _decides(member: Floor[str], choice: HoldChoice, reply: str | None) -> ToldTalkOver:
    """A decision opens, writes its ``reply`` and gives ``choice``, told what the floor tells."""
    told = member.decision_opens()
    assert member.end_hold(choice, told_changes=told.changes) == (Wait(),)
    if reply is not None:
        member.enter(reply)
    return told


@pytest.mark.parametrize(("choice", "reply", "steps", "waiting"), PLAYING_CHOICES)
def test_a_playing_choice_waits_while_a_talk_over_person_speaks_and_plays_once_they_have_ended(
    choice: HoldChoice,
    reply: str | None,
    steps: tuple[object, ...],
    waiting: tuple[str, ...],
) -> None:
    member = held_while_speaking(SECOND)
    _decides(member, choice, reply)
    assert (member.choice_waits, member.choice_may_play) == (True, False)

    member.utterance_ended("ann")

    assert (member.choice_waits, member.choice_may_play) == (True, True)
    assert member.play_choice() == steps
    assert member.choice_waits is False
    assert member.answer_goes_on() is False
    assert member.waiting == waiting


def test_a_waiting_choice_applies_nothing_until_it_plays() -> None:
    member = held_while_speaking(SECOND)
    _decides(member, HoldEnding.ANSWERED, REPLY)

    assert (member.playing, member.waiting) == (FIRST, (SECOND,))
    assert (member.holding, member.hold_id, member.place) == (True, HOLD, place_in())
    assert member.plays is False


def test_a_stopping_choice_never_waits_and_voids_a_waiting_one() -> None:
    member = held_while_speaking(SECOND)
    _decides(member, HoldEnding.ANSWERED, REPLY)

    assert member.end_hold(HoldEnding.STOPPED) == (Drop(FIRST, (SECOND,), CUT),)
    assert member.choice_waits is False


def test_a_finished_line_whose_partial_still_stands_keeps_the_choice_waiting() -> None:
    member = held_while_speaking(SECOND)
    member.hear_finished("ann", "Moment, wie meinst du das?")
    _decides(member, HoldEnding.CONTINUED, None)

    member.hear_finished("ann", "Moment, wie meinst du das?")
    assert member.choice_may_play is False

    member.utterance_ended("ann")
    assert member.play_choice() == (PlayOn(FIRST),)


@pytest.mark.parametrize(
    "begins",
    [
        pytest.param("before the span", id="a person who began before the span"),
        pytest.param("in the span", id="a person whose voice has not reached the bound"),
    ],
)
def test_a_person_who_began_before_the_span_or_has_no_partial_never_delays_a_choice(
    begins: str,
) -> None:
    member = new_floor()
    if begins == "before the span":
        member.utterance_began("ben")
    playing(member, FIRST, SECOND)
    if begins == "in the span":
        member.utterance_began("ben")
    assert lasts(member, "ann", "Moment") is True
    member.sound_stopped_at(place_in(), HOLD)
    member.utterance_ended("ann")

    assert member.end_hold(HoldEnding.CONTINUED) == (PlayOn(FIRST),)


def _a_choice_waits_while_cy_speaks() -> Floor[str]:
    """Ann has said her word and ended; Cy still speaks, wordless, and a choice waits for him."""
    member = playing(new_floor(), FIRST, SECOND)
    lasts(member, "ann", "Moment")
    lasts(member, "cy", "")
    member.sound_stopped_at(place_in(), HOLD)
    member.utterance_ended("ann")
    _decides(member, HoldEnding.CONTINUED, None)
    return member


def _ben_begins_and_lasts_on_the_wire(member: Floor[str], text: str) -> bool:
    member.utterance_began("ben")
    return member.hear_partial("ben", text)


def _ben_lasts_in_a_snapshot(member: Floor[str], text: str) -> bool:
    return member.hear_partials((partial("cy", ""), partial("ben", text)))


@pytest.mark.parametrize(
    "ben_lasts",
    [_ben_begins_and_lasts_on_the_wire, _ben_lasts_in_a_snapshot],
    ids=["on the wire", "in a snapshot"],
)
@pytest.mark.parametrize("ben_words", ["Eine Frage", ""], ids=["with words", "without words"])
def test_a_fresh_speakers_first_partial_after_the_choice_voids_it_and_a_new_hold_stands(
    ben_lasts: Callable[[Floor[str], str], bool], ben_words: str
) -> None:
    member = _a_choice_waits_while_cy_speaks()

    assert ben_lasts(member, ben_words) is True

    assert member.choice_waits is False
    assert (member.playing, member.waiting) == (FIRST, (SECOND,))
    assert (member.holding, member.place) == (True, place_in())
    assert member.hold_id is None
    member.sound_stopped_at(place_in(), "hold-2")
    assert member.hold_id == "hold-2"
    assert member.wordless is (ben_words == "")
    if ben_words == "":
        member.utterance_ended("cy")
        assert member.may_go_on is False
        member.utterance_ended("ben")
        assert member.may_go_on is True


def test_a_first_partial_of_a_person_speaking_at_the_choice_voids_nothing_on_the_wire() -> None:
    member = new_floor()
    playing(member, FIRST, SECOND)
    lasts(member, "ann", "Moment")
    member.utterance_began("ben")
    member.sound_stopped_at(place_in(), HOLD)
    _decides(member, HoldEnding.CONTINUED, None)

    assert member.hear_partial("ben", "Eine Frage") is False
    assert member.choice_waits is True

    assert member.voice_went_on("ben") is True
    assert member.choice_waits is False
    assert member.holding is True
    assert member.hold_id is None


def test_a_voice_that_went_on_changes_nothing_while_no_choice_waits() -> None:
    member = held_while_speaking(SECOND)

    assert member.voice_went_on("ann") is False
    assert member.hold_id == HOLD


@pytest.mark.parametrize(
    ("text", "voids"),
    [
        pytest.param("Moment doch", True, id="new words"),
        pytest.param("moment!", False, id="the same words in other letters"),
    ],
)
def test_in_a_snapshot_changed_words_of_a_person_speaking_at_the_choice_void_it(
    text: str, voids: bool
) -> None:
    member = new_floor()
    playing(member, FIRST, SECOND)
    assert member.hear_partials((partial("ann", "Moment"),)) is True
    member.sound_stopped_at(place_in(), HOLD)
    _decides(member, HoldEnding.CONTINUED, None)

    assert member.hear_partials((partial("ann", text),)) is voids

    assert member.choice_waits is not voids


def test_a_finished_line_never_voids_a_waiting_choice() -> None:
    member = held_while_speaking(SECOND)
    _decides(member, HoldEnding.CONTINUED, None)

    member.hear_finished("ann", "Moment, noch etwas ganz anderes.")
    assert member.choice_waits is True

    member.utterance_ended("ann")
    assert member.play_choice() == (PlayOn(FIRST),)


def _continue(member: Floor[str], told_changes: int) -> object:
    return member.continue_speaking(HOLD, told_changes=told_changes)


def _speak(member: Floor[str], told_changes: int) -> object:
    return member.speak_refusal(HOLD, told_changes)


def _render_ended(member: Floor[str], told_changes: int) -> object:
    ticket = member.render_begins()
    return member.render_ended(ticket, HOLD, told_changes)


def _stop(member: Floor[str], told_changes: int) -> object:
    return member.stop(HOLD, place_in(), told_changes)


def _stopping_choice(member: Floor[str], told_changes: int) -> object:
    return member.end_hold(HoldEnding.STOPPED, told_changes=told_changes)


@pytest.mark.parametrize(
    ("ask", "refusal"),
    [
        pytest.param(_continue, ContinueSpeakingRefusal.STALE, id="continue_speaking"),
        pytest.param(_speak, SpeakRefusal.STALE, id="speak before its render"),
        pytest.param(_render_ended, SpeakRefusal.STALE, id="speak after its render"),
        pytest.param(_stop, StopSpeakingRefusal.STALE, id="stop_speaking"),
        pytest.param(_stopping_choice, Stale(), id="a stopping choice of the loop"),
    ],
)
def test_a_choice_told_fewer_changes_than_counted_is_refused_stale_and_changes_nothing(
    ask: Callable[[Floor[str], int], object], refusal: object
) -> None:
    member = held_while_speaking(SECOND)
    assert member.worded_changes == 1

    assert ask(member, 0) == refusal

    assert (member.holding, member.hold_id, member.playing) == (True, HOLD, FIRST)
    assert (member.waiting, member.choice_waits) == ((SECOND,), False)


@pytest.mark.parametrize("told_changes", [1, 2, None], ids=["told as many", "told more", "unset"])
def test_a_choice_told_as_many_changes_as_counted_or_none_is_not_refused(
    told_changes: int | None,
) -> None:
    member = held_while_speaking(SECOND)

    assert member.continue_speaking(HOLD, told_changes=told_changes) == (Wait(),)


@pytest.mark.parametrize(
    ("texts", "counted"),
    [
        pytest.param(["Moment"], 1, id="a first word"),
        pytest.param(["Moment", "Moment!"], 1, id="the same word in other letters"),
        pytest.param(["Moment", "Moment doch"], 2, id="a new word"),
        pytest.param(["", "..."], 0, id="no word"),
    ],
)
def test_worded_changes_rise_with_each_new_word_of_the_talk_over(
    texts: list[str], counted: int
) -> None:
    member = playing(new_floor(), FIRST)
    member.utterance_began("ann")
    for text in texts:
        member.hear_partial("ann", text)

    assert member.worded_changes == counted


def test_worded_changes_count_a_line_with_new_words_and_not_one_that_repeats_its_partial() -> None:
    member = playing(new_floor(), FIRST)
    lasts(member, "ann", "Moment doch")
    assert member.worded_changes == 1

    member.hear_finished("ann", "Moment doch.")
    assert member.worded_changes == 1

    member.hear_finished("ann", "Moment doch, wie meinst du das?")
    assert member.worded_changes == 2


def test_worded_changes_never_fall_when_a_hold_ends() -> None:
    member = held_while_speaking(SECOND)
    member.utterance_ended("ann")
    member.end_hold(HoldEnding.CONTINUED)
    member.answer_goes_on()

    assert member.worded_changes == 1


def test_a_person_never_of_the_talk_over_adds_no_change() -> None:
    member = playing(new_floor(), FIRST)
    member.utterance_began("ben")
    member.hear_finished("ben", "Eine Frage")

    assert member.worded_changes == 0


def test_a_decision_is_told_the_changes_and_the_partials_that_stand() -> None:
    member = held_while_speaking(SECOND, speakers=("ann", "cy"), text="Moment")

    assert member.decision_opens() == ToldTalkOver(
        changes=2,
        speech=(PartialSpeech(name="ann", text="Moment"), PartialSpeech(name="cy", text="Moment")),
    )


@pytest.mark.parametrize(
    ("line", "repeats"),
    [
        pytest.param("Moment doch.", True, id="the words of the partial it was told"),
        pytest.param("Moment doch, wie meinst du das?", False, id="more words"),
    ],
)
def test_a_line_tells_whether_it_repeats_what_the_decision_was_told(
    line: str, repeats: bool
) -> None:
    member = held_while_speaking(SECOND, text="Moment doch")
    member.decision_opens()
    member.utterance_ended("ann")

    assert member.hear_finished("ann", line) is repeats
    assert member.worded_changes == (1 if repeats else 2)


@pytest.mark.parametrize(
    ("first", "reply", "second", "second_reply", "steps", "waiting"),
    [
        pytest.param(
            HoldEnding.ANSWERED,
            REPLY,
            HoldEnding.CONTINUED,
            None,
            (PlayOn(FIRST),),
            (SECOND,),
            id="a continue replaces a speak",
        ),
        pytest.param(
            HoldEnding.CONTINUED,
            None,
            HoldEnding.ANSWERED,
            "Nein.",
            (Drop(FIRST, (SECOND,), CUT),),
            ("Nein.",),
            id="a speak replaces a continue",
        ),
    ],
)
def test_a_second_choice_for_the_hold_replaces_the_waiting_one(
    first: HoldChoice,
    reply: str | None,
    second: HoldChoice,
    second_reply: str | None,
    steps: tuple[object, ...],
    waiting: tuple[str, ...],
) -> None:
    member = held_while_speaking(SECOND)
    told = _decides(member, first, reply)

    assert member.end_hold(second, told_changes=told.changes) == (Wait(),)
    if second_reply is not None:
        member.enter(second_reply)
    member.utterance_ended("ann")

    assert member.play_choice() == steps
    member.answer_goes_on()
    assert member.waiting == waiting


def test_a_stale_second_choice_changes_nothing_and_the_waiting_one_stands() -> None:
    member = held_while_speaking(SECOND)
    told = _decides(member, HoldEnding.ANSWERED, REPLY)
    member.hear_partial("ann", "Moment doch noch")

    assert member.end_hold(HoldEnding.CONTINUED, told_changes=told.changes) == Stale()

    assert member.choice_waits is True
    member.utterance_ended("ann")
    assert member.play_choice() == (Drop(FIRST, (SECOND,), CUT),)
    member.answer_goes_on()
    assert member.waiting == (REPLY,)


def test_a_stop_while_a_choice_waits_voids_it_and_drops_the_reply_with_the_queue() -> None:
    member = held_while_speaking(SECOND)
    _decides(member, HoldEnding.ANSWERED, REPLY)

    assert member.stop(HOLD, place_in()) == Drop(FIRST, (SECOND, REPLY), CUT)

    assert (member.choice_waits, member.holding) == (False, False)


def test_a_new_decision_drops_the_reply_left_by_a_stale_one() -> None:
    member = held_while_speaking(SECOND)
    told = member.decision_opens()
    member.enter("Alt.")
    member.hear_partial("ann", "Moment doch noch")
    assert member.end_hold(HoldEnding.ANSWERED, told_changes=told.changes) == Stale()

    told = member.decision_opens()
    member.enter("Neu.")
    member.end_hold(HoldEnding.ANSWERED, told_changes=told.changes)
    member.utterance_ended("ann")
    member.play_choice()
    member.answer_goes_on()

    assert member.waiting == ("Neu.",)


def test_a_person_who_starts_after_the_choice_played_holds_again_with_a_new_id() -> None:
    member = held_while_speaking(SECOND)
    assert member.continue_speaking(HOLD) == (Wait(),)
    member.utterance_ended("ann")

    assert member.choice_may_play is True
    assert member.play_choice() == (PlayOn(FIRST),)
    assert member.answer_goes_on() is False
    assert lasts(member, "ben", "Eine Frage") is True
    assert member.hold_id is None


def test_a_person_whose_new_voice_lasts_the_bound_after_the_choice_played_holds_again() -> None:
    member = held_while_speaking(SECOND)
    assert member.end_hold(HoldEnding.ANSWERED) == (Wait(),)
    member.utterance_ended("ann")
    member.play_choice()
    assert member.answer_goes_on() is False
    member.enter(REPLY)

    assert lasts(member, "ann", "noch mehr") is True
