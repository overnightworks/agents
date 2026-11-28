"""`agents.floor` decides, on one record, how a runner's sentences are held, refused, stopped and
continued; every case drives the floor through its events and reads its steps and answers."""

from collections.abc import Callable

import pytest

from agents.floor import (
    Drop,
    Floor,
    HoldChoice,
    HoldEnding,
    PlayOn,
    Start,
    Wait,
    cut_line,
    leaves_the_turn,
)
from agents.member_speaking import (
    ContinueSpeakingRefusal,
    HoldPlace,
    SpeakRefusal,
    StopSpeakingRefusal,
    hold_place,
)
from tests.support.floor_support import (
    CUT_AT,
    FIRST,
    HOLD,
    SECOND,
    THIRD,
    held,
    held_at_once,
    lasts,
    loop_floor,
    partial,
    place_in,
    playing,
    rendered,
    stopping,
    wire_floor,
)

BETWEEN = hold_place(None, None)


def test_sentences_start_in_the_order_they_are_entered() -> None:
    member = wire_floor()
    for sentence in (FIRST, SECOND, THIRD):
        rendered(member, sentence)
    started = []
    while (step := member.next_start()) is not None:
        started.append(step)
        member.sentence_ended()
    assert started == [Start(FIRST), Start(SECOND), Start(THIRD)]


def test_a_sentence_answering_the_hold_starts_first_after_the_held_rest_is_dropped() -> None:
    member = held(FIRST, SECOND)
    ticket = member.render_begins()
    assert member.render_ended(ticket, HOLD) is None
    steps = member.end_hold(HoldEnding.ANSWERED)
    assert member.answer_goes_on() is False
    member.enter(THIRD)
    assert steps == (Drop(FIRST, (SECOND,), FIRST[:CUT_AT]),)
    member.sentence_ended()
    assert member.next_start() == Start(THIRD)


@pytest.mark.parametrize(
    "entered_before_the_choice",
    [
        pytest.param(True, id="a reply written while the hold decides"),
        pytest.param(False, id="a reply entered after the choice"),
    ],
)
def test_a_short_line_cuts_the_held_sentence_and_goes_ahead_of_the_waiting_ones(
    entered_before_the_choice: bool,
) -> None:
    member = held(FIRST, SECOND)
    member.decision_opens()
    if entered_before_the_choice:
        member.enter(THIRD)
    steps = member.end_hold(HoldEnding.SAID_THEN_CONTINUED)
    assert member.answer_goes_on() is False
    if not entered_before_the_choice:
        member.enter(THIRD)
    member.reply_ended()
    assert steps == (Drop(FIRST, (), FIRST[:CUT_AT]),)
    started = []
    member.sentence_ended()
    while (step := member.next_start()) is not None:
        started.append(step)
        member.sentence_ended()
    assert started == [Start(THIRD), Start(SECOND)]


def test_later_reply_sentences_of_a_short_line_stay_ahead_of_the_waiting_ones_until_it_ends() -> (
    None
):
    later = "Sieben acht."
    member = held(FIRST, SECOND)
    member.decision_opens()
    member.end_hold(HoldEnding.SAID_THEN_CONTINUED)
    member.answer_goes_on()
    member.sentence_ended()
    assert member.next_start() is None
    member.enter(THIRD)
    assert member.next_start() == Start(THIRD)
    member.sentence_ended()
    assert member.next_start() is None
    member.enter(later)
    assert member.next_start() == Start(later)
    member.sentence_ended()
    member.reply_ended()
    assert member.next_start() == Start(SECOND)


def test_a_stop_after_a_short_line_drops_the_sentences_that_wait_behind_its_reply() -> None:
    member = held(FIRST, SECOND)
    member.end_hold(HoldEnding.SAID_THEN_CONTINUED)
    member.answer_goes_on()
    member.enter(THIRD)
    member.sentence_ended()
    stopped = member.stop(None, BETWEEN)
    assert stopped == Drop(None, (THIRD, SECOND), None)
    assert member.next_start() is None


@pytest.mark.parametrize(
    "choice",
    [
        HoldEnding.CONTINUED,
        HoldEnding.FINISHED,
        HoldEnding.SAID_THEN_CONTINUED,
        HoldEnding.ANSWERED,
        HoldEnding.STOPPED,
    ],
)
def test_a_choice_leaves_the_held_turn_exactly_when_it_drops_the_held_sentence(
    choice: HoldChoice,
) -> None:
    steps = held(FIRST, SECOND).end_hold(choice)
    drops_the_held = any(isinstance(step, Drop) and step.sentence == FIRST for step in steps)
    assert drops_the_held is leaves_the_turn(choice)


def test_a_continue_with_finish_plays_the_held_sentence_on_and_drops_the_waiting_ones() -> None:
    member = held(FIRST, SECOND)
    assert member.continue_speaking(HOLD, finish=True) == (
        Drop(None, (SECOND,), None),
        PlayOn(FIRST),
    )
    member.sentence_ended()
    assert member.next_start() is None


@pytest.mark.parametrize(
    "drop",
    [
        pytest.param(lambda member: member.stop(HOLD, place_in()), id="a stop"),
        pytest.param(
            lambda member: member.end_hold(HoldEnding.ANSWERED)[0],
            id="an answer",
        ),
    ],
)
def test_a_drop_names_each_waiting_sentence_a_stop_or_an_answer_drops(
    drop: Callable[[Floor[str]], object],
) -> None:
    dropped = drop(held(FIRST, SECOND, THIRD))
    assert isinstance(dropped, Drop)
    assert dropped.waiting == (SECOND, THIRD)


@pytest.mark.parametrize(
    ("heard_to", "line"),
    [
        pytest.param(CUT_AT, FIRST[:CUT_AT], id="the heard part"),
        pytest.param(None, FIRST, id="the whole sentence when the app cannot tell"),
        pytest.param(0, None, id="none when no seat heard a word"),
    ],
)
def test_a_cut_line_is_the_heard_part_of_the_sentence(
    heard_to: int | None, line: str | None
) -> None:
    place = hold_place(FIRST, heard_to)
    assert cut_line(place) == line
    stopped = held(FIRST, heard_to=heard_to).stop(None, place)
    assert isinstance(stopped, Drop)
    assert (stopped.sentence, stopped.cut) == (FIRST, line)


def test_a_place_between_sentences_has_no_cut_line() -> None:
    assert cut_line(BETWEEN) is None


def test_a_held_sentence_yields_nothing_until_it_plays_on_and_ends() -> None:
    member = held(FIRST, SECOND)
    assert (member.playing, member.plays) == (FIRST, False)
    assert member.continue_speaking(HOLD) == (PlayOn(FIRST),)
    assert (member.playing, member.plays) == (FIRST, True)
    member.sentence_ended()
    assert (member.playing, member.plays) == (None, False)
    assert member.next_start() == Start(SECOND)


def _a_sentence_waits() -> Floor[str]:
    member = wire_floor()
    rendered(member, FIRST)
    return member


def _a_sentence_renders() -> Floor[str]:
    member = wire_floor()
    member.render_begins()
    return member


@pytest.mark.parametrize(
    ("scene", "holds"),
    [
        pytest.param(lambda: playing(wire_floor()), True, id="a sentence plays"),
        pytest.param(_a_sentence_waits, True, id="a sentence waits"),
        pytest.param(_a_sentence_renders, True, id="a sentence renders"),
        pytest.param(wire_floor, False, id="the member is silent"),
    ],
)
def test_a_lasting_voice_holds_a_member_that_plays_waits_or_renders(
    scene: Callable[[], Floor[str]], holds: bool
) -> None:
    member = scene()
    assert lasts(member, "ann") is holds
    assert member.holding is holds


def test_a_turn_that_began_before_the_span_never_holds() -> None:
    member = wire_floor()
    member.utterance_began("ann")
    playing(member)
    assert member.hear_partial("ann", "Moment") is False
    assert member.holding is False


def test_the_span_closes_when_nothing_is_left() -> None:
    member = playing(wire_floor())
    member.utterance_began("ann")
    member.sentence_ended()
    assert member.hear_partial("ann", "Moment") is False


def test_a_person_still_speaking_across_a_close_and_a_new_opening_does_not_hold() -> None:
    member = playing(wire_floor())
    member.utterance_began("ann")
    member.sentence_ended()
    playing(member, SECOND)
    assert member.hear_partial("ann", "Moment") is False


def test_sentences_left_waiting_after_an_answer_ended_open_no_span() -> None:
    member = loop_floor()
    member.answer_begins()
    member.enter(FIRST)
    member.enter(SECOND)
    assert member.next_start() == Start(FIRST)
    member.sentence_ended()
    member.answer_ends()
    assert member.hear_partials((partial("ann", "Moment"),)) is False
    assert member.waiting == (SECOND,)


def test_after_a_continue_only_a_fresh_voice_holds_again() -> None:
    member = held(FIRST, SECOND)
    assert member.continue_speaking(HOLD) == (PlayOn(FIRST),)
    assert member.hear_partial("ann", "noch mehr") is False
    assert member.holding is False
    assert lasts(member, "ben") is True


def test_after_an_answer_a_person_already_speaking_does_not_hold_again() -> None:
    member = held(FIRST)
    member.end_hold(HoldEnding.ANSWERED)
    assert member.answer_goes_on() is False
    member.enter(SECOND)
    assert member.hear_partial("ann", "noch mehr") is False


def test_plays_is_true_while_a_started_sentence_has_not_ended_and_the_member_is_not_held() -> None:
    member = wire_floor()
    rendered(member, FIRST)
    assert member.plays is False
    assert member.next_start() == Start(FIRST)
    assert member.plays is True
    lasts(member, "ann", "Moment")
    assert member.plays is True
    member.sound_stopped_at(place_in(), HOLD)
    assert member.plays is False
    member.continue_speaking(HOLD)
    assert member.plays is True
    member.sentence_ended()
    assert member.plays is False


def test_while_stopping_no_id_stands_an_id_less_sentence_waits_and_any_id_is_refused() -> None:
    member = stopping(FIRST)
    assert member.hold_id is None
    assert member.speak_refusal(None) is None
    assert member.speak_refusal(HOLD) is SpeakRefusal.NOT_INTERRUPTED
    rendered(member, SECOND)
    assert member.next_start() == Wait()
    assert member.waiting == (SECOND,)


def test_a_stop_while_stopping_leaves_the_member_silent_and_no_hold_arises() -> None:
    member = stopping(FIRST, SECOND)
    stopped = member.stop(None, place_in())
    assert stopped == Drop(FIRST, (SECOND,), FIRST[:CUT_AT])
    member.sentence_ended()
    assert member.holding is False
    assert member.stop(None, BETWEEN) is StopSpeakingRefusal.NOTHING_PLAYING
    assert lasts(member, "ben") is False


def test_a_late_place_report_after_a_stop_changes_nothing() -> None:
    member = stopping(FIRST)
    member.stop(None, place_in())
    member.sentence_ended()
    member.sound_stopped_at(place_in(), HOLD)
    assert (member.hold_id, member.holding) == (None, False)


def test_a_hold_is_held_under_the_id_from_the_place_report() -> None:
    member = stopping(FIRST, SECOND)
    place = place_in()
    member.sound_stopped_at(place, HOLD)
    assert (member.hold_id, member.place) == (HOLD, place)
    member.sound_stopped_at(BETWEEN, "hold-2")
    assert (member.hold_id, member.place) == (HOLD, place)
    assert member.next_start() == Wait()


def test_a_hold_with_nothing_playing_is_held_at_once_and_the_report_gives_the_id() -> None:
    member = held_at_once(FIRST)
    assert member.place == BETWEEN
    assert member.hold_id == HOLD
    assert member.next_start() == Wait()


def test_a_sentence_that_played_out_while_stopping_leaves_the_hold_stopping_until_the_report() -> (
    None
):
    member = stopping(FIRST, SECOND)
    member.sentence_ended()
    assert member.plays is False
    assert member.hold_id is None
    member.sound_stopped_at(BETWEEN, HOLD)
    assert member.hold_id == HOLD
    assert member.next_start() == Wait()


def _states() -> dict[str, Callable[[], Floor[str]]]:
    return {
        "silent": wire_floor,
        "playing": lambda: playing(wire_floor()),
        "stopping": stopping,
        "held": held,
    }


@pytest.mark.parametrize("state", ["silent", "playing", "stopping", "held"])
def test_a_speak_naming_another_id_is_refused_in_every_state_and_changes_nothing(
    state: str,
) -> None:
    member = _states()[state]()
    before = (member.holding, member.hold_id, member.waiting, member.renders)
    assert member.speak_refusal("another") is SpeakRefusal.NOT_INTERRUPTED
    assert before == (member.holding, member.hold_id, member.waiting, member.renders)


@pytest.mark.parametrize(
    ("state", "refusal"),
    [
        pytest.param("held", SpeakRefusal.HELD, id="held-refuses"),
        pytest.param("stopping", None, id="stopping-passes"),
        pytest.param("playing", None, id="playing-passes"),
        pytest.param("silent", None, id="silent-passes"),
    ],
)
def test_a_speak_without_an_id_is_refused_held_only_while_held(
    state: str, refusal: SpeakRefusal | None
) -> None:
    assert _states()[state]().speak_refusal(None) is refusal


def test_a_speak_naming_the_standing_id_passes() -> None:
    assert held().speak_refusal(HOLD) is None
    assert wire_floor().speak_refusal(HOLD) is SpeakRefusal.NOT_INTERRUPTED


def test_a_ticket_ended_after_a_stop_answers_stopped_before_the_id_is_checked() -> None:
    member = held(FIRST)
    ticket = member.render_begins()
    assert isinstance(member.stop(HOLD, place_in()), Drop)
    assert member.render_ended(ticket, HOLD) is SpeakRefusal.STOPPED
    assert member.render_ended(ticket) is SpeakRefusal.STOPPED


def test_a_ticket_ended_after_an_answer_dropped_it_answers_stopped() -> None:
    member = held(FIRST)
    other = member.render_begins()
    member.end_hold(HoldEnding.ANSWERED)
    assert member.render_ended(other) is SpeakRefusal.STOPPED


def test_a_hold_that_arose_during_an_id_less_render_refuses_nothing() -> None:
    member = playing(wire_floor())
    ticket = member.render_begins()
    assert lasts(member, "ann", "Moment")
    member.sound_stopped_at(place_in(), HOLD)
    assert member.render_ended(ticket) is None
    member.enter(SECOND)
    assert member.waiting == (SECOND,)


def test_a_continue_during_the_render_of_an_answer_leaves_the_answer_not_interrupted() -> None:
    member = held(FIRST)
    ticket = member.render_begins()
    assert member.continue_speaking(HOLD) == (PlayOn(FIRST),)
    assert member.render_ended(ticket, HOLD) is SpeakRefusal.NOT_INTERRUPTED


def _cut() -> tuple[Floor[str], str | None, HoldPlace]:
    return playing(wire_floor(), FIRST), None, place_in()


def _only_waiting() -> tuple[Floor[str], str | None, HoldPlace]:
    member = wire_floor()
    rendered(member, FIRST)
    return member, None, BETWEEN


def _only_renders() -> tuple[Floor[str], str | None, HoldPlace]:
    member = wire_floor()
    member.render_begins()
    return member, None, BETWEEN


def _held_never_started() -> tuple[Floor[str], str | None, HoldPlace]:
    return held_at_once(FIRST), HOLD, BETWEEN


def _held_heard_by_no_seat() -> tuple[Floor[str], str | None, HoldPlace]:
    return held(FIRST, heard_to=0), HOLD, place_in(heard_to=0)


def _held_ended_while_held() -> tuple[Floor[str], str | None, HoldPlace]:
    member = held(FIRST, SECOND)
    member.sentence_ended()
    return member, HOLD, BETWEEN


@pytest.mark.parametrize(
    ("scene", "expected"),
    [
        pytest.param(_cut, Drop(FIRST, (), FIRST[:CUT_AT]), id="a cut sentence"),
        pytest.param(_only_waiting, Drop(None, (FIRST,), None), id="only waiting ones"),
        pytest.param(_only_renders, Drop(None, (), None), id="only renders"),
        pytest.param(
            _held_never_started, Drop(None, (FIRST,), None), id="a held one never started"
        ),
        pytest.param(_held_heard_by_no_seat, Drop(FIRST, (), None), id="a held one no seat heard"),
        pytest.param(
            _held_ended_while_held,
            Drop(None, (SECOND,), None),
            id="a held one that ended while held",
        ),
    ],
)
def test_a_stop_answers_what_it_cut_or_dropped(
    scene: Callable[[], tuple[Floor[str], str | None, HoldPlace]], expected: Drop[str]
) -> None:
    member, interrupted, place = scene()
    assert member.stop(interrupted, place) == expected
    assert member.holding is False
    assert member.renders == 0


def test_a_stop_of_a_silent_member_answers_nothing_playing() -> None:
    assert wire_floor().stop(None, BETWEEN) is StopSpeakingRefusal.NOTHING_PLAYING


@pytest.mark.parametrize("state", ["silent", "playing", "stopping"])
def test_a_stop_naming_an_id_that_does_not_stand_is_refused_and_changes_nothing(state: str) -> None:
    member = _states()[state]()
    assert member.stop(HOLD, BETWEEN) is StopSpeakingRefusal.NOT_INTERRUPTED
    assert member.playing == (None if state == "silent" else FIRST)


def test_a_continue_plays_the_held_sentence_on_then_the_waiting_ones() -> None:
    member = held(FIRST, SECOND)
    assert member.continue_speaking(HOLD) == (PlayOn(FIRST),)
    assert member.holding is False
    member.sentence_ended()
    assert member.next_start() == Start(SECOND)


def _ended_held(*waiting: str) -> Floor[str]:
    member = held(FIRST, *waiting)
    member.sentence_ended()
    return member


@pytest.mark.parametrize(
    "scene",
    [
        pytest.param(lambda: held_at_once(SECOND), id="no sentence started"),
        pytest.param(lambda: _ended_held(SECOND), id="the started one ended while held"),
    ],
)
def test_a_continue_starts_the_first_waiting_sentence_when_none_is_held(
    scene: Callable[[], Floor[str]],
) -> None:
    member = scene()
    assert member.continue_speaking(HOLD) == ()
    assert member.next_start() == Start(SECOND)


def test_a_continue_naming_another_id_is_refused_and_the_hold_stands() -> None:
    member = held(FIRST)
    assert member.continue_speaking("another") is ContinueSpeakingRefusal.NOT_INTERRUPTED
    assert member.hold_id == HOLD
    assert wire_floor().continue_speaking(HOLD) is ContinueSpeakingRefusal.NOT_INTERRUPTED


def _wordless() -> Floor[str]:
    member = playing(wire_floor())
    assert lasts(member, "ann", "")
    member.sound_stopped_at(place_in(), HOLD)
    return member


def test_a_hold_is_wordless_from_held_until_the_first_word_and_never_again() -> None:
    member = _wordless()
    assert member.wordless is True
    member.hear_partial("ann", "...")
    assert member.wordless is True
    member.hear_partial("ann", "Hallo")
    assert member.wordless is False
    member.decision_opens()
    assert member.wordless is False


def test_a_word_heard_while_stopping_means_the_hold_is_never_wordless() -> None:
    member = playing(wire_floor())
    assert lasts(member, "ann", "Moment")
    member.sound_stopped_at(place_in(), HOLD)
    assert member.wordless is False


def test_a_wordless_hold_may_go_on_once_nobody_speaks_and_went_on_names_it() -> None:
    member = _wordless()
    assert member.may_go_on is False
    member.utterance_ended("ann")
    assert member.may_go_on is True
    assert member.go_on() == (PlayOn(FIRST),)
    assert member.answer_goes_on() is False
    assert member.went_on == HOLD


def test_went_on_stays_until_a_later_hold_goes_on() -> None:
    member = _wordless()
    member.utterance_ended("ann")
    member.go_on()
    member.answer_goes_on()
    assert lasts(member, "ben") is True
    member.sound_stopped_at(place_in(), "hold-2")
    assert member.went_on == HOLD
    member.utterance_ended("ben")
    member.go_on()
    assert member.went_on == "hold-2"


def test_a_line_of_the_talk_over_counts_before_the_turns_end_is_judged() -> None:
    member = _wordless()
    member.hear_finished("ann", "Warte")
    assert member.nobody_speaks is False
    member.utterance_ended("ann")
    assert (member.wordless, member.may_go_on) == (False, False)
    assert member.nobody_speaks is True
    member.hear_finished("zoe", "Hallo")
    assert member.nobody_speaks is True


def test_a_line_after_the_persons_turn_ended_still_counts() -> None:
    member = _wordless()
    member.utterance_ended("ann")
    member.hear_finished("ann", "Warte")
    assert member.wordless is False


def test_a_line_of_a_person_never_in_the_talk_over_neither_counts_nor_blocks_the_going_on() -> None:
    member = wire_floor()
    member.utterance_began("ben")
    playing(member)
    assert lasts(member, "ann", "")
    member.sound_stopped_at(place_in(), HOLD)
    member.hear_finished("ben", "Warte")
    member.utterance_ended("ben")
    member.utterance_ended("ann")
    assert member.may_go_on is True


@pytest.mark.parametrize("hold", [stopping, _wordless], ids=["stopping", "held"])
def test_a_person_who_starts_while_stopping_or_held_joins_the_talk_over_and_opens_no_second_hold(
    hold: Callable[[], Floor[str]],
) -> None:
    member = hold()
    member.utterance_began("ben")
    assert member.hear_partial("ben", "Hallo") is False
    member.utterance_ended("ann")
    assert member.nobody_speaks is False
    assert member.worded is True


def test_two_people_under_one_shown_name_with_different_keys_are_two() -> None:
    member = playing(wire_floor())
    assert lasts(member, "seat-1") is True
    assert lasts(member, "seat-2") is False
    member.utterance_ended("seat-1")
    assert (member.nobody_speaks, member.standing) == (False, True)
    member.utterance_ended("seat-2")
    assert (member.nobody_speaks, member.standing) == (True, False)


def test_renders_counts_the_open_tickets_and_no_dropped_or_ended_one() -> None:
    member = wire_floor()
    first, second, third = (member.render_begins() for _ in range(3))
    assert member.renders == 3
    member.render_ended(first)
    assert member.renders == 2
    member.stop(None, BETWEEN)
    assert member.renders == 0
    assert member.render_ended(second) is SpeakRefusal.STOPPED
    assert member.render_ended(third) is SpeakRefusal.STOPPED
    assert member.renders == 0


def test_a_sentence_ended_in_the_step_of_its_start_is_never_a_later_cut() -> None:
    member = wire_floor()
    rendered(member, FIRST)
    assert member.next_start() == Start(FIRST)
    member.sentence_ended()
    member.render_begins()
    assert lasts(member, "ann", "Moment") is True
    assert member.place == BETWEEN
    stopped = member.stop(None, BETWEEN)
    assert stopped == Drop(None, (), None)


def test_no_empty_drop_and_a_finish_drops_only_the_waiting_sentences() -> None:
    member = loop_floor()
    member.answer_begins()
    member.enter(FIRST)
    member.next_start()
    assert member.hear_partials((partial("ann", "Moment"),)) is True
    member.sound_stopped_at(place_in())
    member.hear_partials(())
    member.enter(SECOND)
    steps = member.end_hold(HoldEnding.FINISHED)
    assert steps == (Drop(None, (SECOND,), None), PlayOn(FIRST))
    assert member.end_hold(HoldEnding.CONTINUED) == (PlayOn(FIRST),)


def _heard_again_after_its_end(member: Floor[str]) -> None:
    member.utterance_began("ann")
    member.utterance_ended("ann")


@pytest.mark.parametrize(
    "scene",
    [
        pytest.param(lambda member: None, id="a-person-speaking-since-before-the-floor-existed"),
        pytest.param(_heard_again_after_its_end, id="a-turn-heard-again-after-its-end"),
    ],
)
def test_a_voice_whose_utterance_began_before_holds_nothing_and_counts_as_speaking(
    scene: Callable[[Floor[str]], None],
) -> None:
    member = playing(wire_floor())
    scene(member)
    assert member.hear_partial("ann", "Moment") is False
    assert member.nobody_speaks is False
    assert (member.standing, member.holding) == (False, False)


def test_a_span_a_render_opened_stays_open_through_the_step_that_ends_the_ticket() -> None:
    member = wire_floor()
    ticket = member.render_begins()
    member.utterance_began("ann")
    assert member.render_ended(ticket) is None
    member.enter(FIRST)
    assert member.hear_partial("ann", "Moment") is True


def test_a_ticket_that_ends_with_nothing_entered_leaves_the_member_silent() -> None:
    member = wire_floor()
    ticket = member.render_begins()
    member.utterance_began("ann")
    assert member.render_ended(ticket) is None
    assert member.hear_partial("ann", "Moment") is False
    assert lasts(member, "ben") is False
    assert member.stop(None, BETWEEN) is StopSpeakingRefusal.NOTHING_PLAYING


def test_a_name_the_snapshot_lists_again_after_its_line_begins_anew_during_the_answer() -> None:
    member = loop_floor()
    assert member.hear_partials((partial("ann", "Ja"),)) is False
    member.answer_begins()
    member.enter(FIRST)
    member.next_start()
    assert member.hear_partials((partial("ann", "Ja"),)) is False
    member.hear_finished("ann", "Ja.")
    assert member.hear_partials((partial("ann", "Moment"),)) is True
    assert member.standing is True


def test_a_name_the_snapshot_lists_again_outside_an_answer_holds_nothing() -> None:
    member = loop_floor()
    member.answer_begins()
    member.enter(FIRST)
    member.next_start()
    member.hear_partials((partial("ann", "Ja"),))
    member.answer_ends()
    member.hear_finished("ann", "Ja.")
    assert member.hear_partials((partial("ann", "Moment"),)) is False
    assert member.standing is False
