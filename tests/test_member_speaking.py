"""What an agent and an app say about speaking: style, stopping and being talked over."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from agents.figure_vocabulary import FigureMood
from agents.member import (
    CONTINUE_SPEAKING_TOOL,
    STOP_SPEAKING_TOOL,
    ContinuedSpeaking,
    ContinueSpeakingArguments,
    DroppedSpeaking,
    HoldPlace,
    HoldPoint,
    Interrupted,
    PlaceRef,
    PlaceWindow,
    SpeakAnswer,
    SpeakArguments,
    Spoken,
    StoppedSpeaking,
    StopSpeakingAnswer,
    StopSpeakingArguments,
    TalkedOver,
    VoiceState,
    Written,
    hold_place,
)
from tests.support.member_wire import (
    continue_wire,
    entry,
    interrupted_wire,
    speak_wire,
    spoken_wire,
    stop_wire,
    talked_over_wire,
    talked_window_wire,
    window_from,
    written_wire,
)

PLACE = PlaceRef(kind="room", id="r-1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("language", "en", id="language"),
        pytest.param("style", "louder and angrier", id="style"),
        pytest.param("interrupted", "h-1", id="interrupted"),
        *[pytest.param("mood", mood.value, id=f"mood-{mood.value}") for mood in FigureMood],
    ],
)
def test_speak_arguments_name_the_optional_field_of_the_sentence_when_given(
    field: str, value: str
) -> None:
    arguments = SpeakArguments(place=PLACE, text="Shall we start?", **{field: value})

    assert json.loads(arguments.model_dump_json()) == speak_wire(**{field: value})
    assert SpeakArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize("field", ["language", "style", "mood", "interrupted"])
def test_speak_arguments_without_an_optional_field_dump_without_its_key(field: str) -> None:
    arguments = SpeakArguments.model_validate_json(json.dumps(speak_wire()))

    assert getattr(arguments, field) is None
    assert field not in json.loads(arguments.model_dump_json())


@pytest.mark.parametrize("field", ["style", "interrupted"])
@pytest.mark.parametrize("value", ["", 1, ["calm"]], ids=["empty", "number", "list"])
def test_speak_arguments_with_a_malformed_text_field_are_refused(field: str, value: object) -> None:
    wire_text = json.dumps(speak_wire(**{field: value}))

    with pytest.raises(ValidationError):
        SpeakArguments.model_validate_json(wire_text)


@pytest.mark.parametrize(
    "mood",
    ["joyful", "", "HAPPY", 1, ["happy"]],
    ids=["unknown", "empty", "other-case", "number", "list"],
)
def test_speak_arguments_with_a_mood_outside_figures_set_are_refused(mood: object) -> None:
    wire_text = json.dumps(speak_wire(mood=mood))

    with pytest.raises(ValidationError):
        SpeakArguments.model_validate_json(wire_text)


def test_stop_speaking_arguments_carry_the_place_on_the_wire() -> None:
    arguments = StopSpeakingArguments(place=PLACE)

    assert json.loads(arguments.model_dump_json()) == {"place": {"kind": "room", "id": "r-1"}}
    assert StopSpeakingArguments.model_validate_json(arguments.model_dump_json()) == arguments
    assert STOP_SPEAKING_TOOL == "stop_speaking"


@pytest.mark.parametrize(
    "wire",
    [
        {},
        {"place": "room/r-1"},
        {"place": {"kind": "room"}},
        {"place": {"kind": "room", "id": "r"}, "extra": 1},
    ],
    ids=["no-place", "text-place", "place-without-id", "unknown-member"],
)
def test_stop_speaking_arguments_with_a_malformed_field_are_refused(wire: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StopSpeakingArguments.model_validate_json(json.dumps(wire))


def test_stopped_speaking_names_the_sentence_it_cut() -> None:
    stopped = StoppedSpeaking.model_validate_json(json.dumps({"cut": "Shall we start?"}))

    assert stopped.cut == "Shall we start?"


@pytest.mark.parametrize(
    "wire",
    [{}, {"cut": ""}, {"cut": 1}, {"cut": "x", "dropped": 2}],
    ids=["no-cut", "empty-cut", "number-cut", "unknown-member"],
)
def test_stopped_speaking_with_a_malformed_field_is_refused(wire: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        StoppedSpeaking.model_validate_json(json.dumps(wire))


def test_stop_speaking_arguments_name_the_hold_when_given() -> None:
    arguments = StopSpeakingArguments.model_validate_json(json.dumps(stop_wire(interrupted="h-1")))

    assert arguments.interrupted == "h-1"
    assert json.loads(arguments.model_dump_json()) == stop_wire(interrupted="h-1")
    assert StopSpeakingArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize("interrupted", ["", 1, ["h-1"]], ids=["empty", "number", "list"])
def test_stop_speaking_arguments_with_a_malformed_hold_are_refused(interrupted: object) -> None:
    with pytest.raises(ValidationError):
        StopSpeakingArguments.model_validate_json(json.dumps(stop_wire(interrupted=interrupted)))


def test_continue_speaking_arguments_name_the_hold_on_the_wire() -> None:
    arguments = ContinueSpeakingArguments.model_validate_json(json.dumps(continue_wire()))

    assert json.loads(arguments.model_dump_json()) == continue_wire()
    assert CONTINUE_SPEAKING_TOOL == "continue_speaking"


@pytest.mark.parametrize(
    "wire",
    [
        {"place": {"kind": "room", "id": "r-1"}},
        continue_wire(interrupted=""),
        continue_wire(interrupted=1),
        continue_wire(extra=1),
        {"interrupted": "h-1"},
    ],
    ids=["no-id", "empty-id", "number-id", "unknown-member", "no-place"],
)
def test_continue_speaking_arguments_with_a_malformed_field_are_refused(
    wire: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ContinueSpeakingArguments.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("wire", "record", "field"),
    [
        ({"continued": True}, ContinuedSpeaking, "continued"),
        ({"dropped": True}, DroppedSpeaking, "dropped"),
    ],
    ids=["continued", "dropped"],
)
def test_a_hold_answer_reads_back_from_its_json_as_the_marker_alone(
    wire: dict[str, Any], record: type[ContinuedSpeaking | DroppedSpeaking], field: str
) -> None:
    answer = record.model_validate_json(json.dumps(wire))

    assert json.loads(answer.model_dump_json()) == {field: True}


@pytest.mark.parametrize("record", [ContinuedSpeaking, DroppedSpeaking])
@pytest.mark.parametrize(
    "marker",
    [False, "yes", 1, None, {}, []],
    ids=["false", "text", "number", "null", "obj", "list"],
)
def test_a_hold_answer_whose_marker_is_not_exactly_true_is_refused(
    record: type[ContinuedSpeaking | DroppedSpeaking], marker: object
) -> None:
    field = "continued" if record is ContinuedSpeaking else "dropped"

    with pytest.raises(ValidationError):
        record.model_validate_json(json.dumps({field: marker}))


@pytest.mark.parametrize(
    ("wire", "record"),
    [({"cut": "Shall we"}, StoppedSpeaking), ({"dropped": True}, DroppedSpeaking)],
    ids=["stopped", "dropped"],
)
def test_a_stop_answer_reads_stopped_and_dropped_each_as_its_own_record(
    wire: dict[str, Any], record: type[StoppedSpeaking | DroppedSpeaking]
) -> None:
    answer = TypeAdapter(StopSpeakingAnswer).validate_json(json.dumps(wire))

    assert type(answer) is record


@pytest.mark.parametrize(
    "wire",
    [{"cut": "Shall we", "dropped": True}, {}],
    ids=["both", "neither"],
)
def test_a_stop_answer_that_is_both_or_neither_is_refused(wire: dict[str, Any]) -> None:
    answer_adapter = TypeAdapter(StopSpeakingAnswer)
    payload = json.dumps(wire)

    with pytest.raises(ValidationError):
        answer_adapter.validate_json(payload)


def test_a_window_reads_its_hold_back_from_its_json_unchanged() -> None:
    window = window_from(
        talked_window_wire(
            talked_over=talked_over_wire(playing=False), interrupted=interrupted_wire()
        )
    )

    assert window.interrupted == Interrupted(id="h-1")
    assert json.loads(window.model_dump_json())["interrupted"] == interrupted_wire()
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


A_SENTENCE = "Guten Tag, wie geht es?"
AN_EMOJI_SENTENCE = "Hallo \U0001f600\U0001f600"
"""Eight code points, ten UTF-16 units and thirteen UTF-8 bytes."""


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"at": HoldPoint.BETWEEN_SENTENCES}, id="between"),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": A_SENTENCE}, id="in sentence, cannot tell"
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": A_SENTENCE, "heard_to": 0},
            id="in sentence, none heard",
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": A_SENTENCE, "heard_to": len(A_SENTENCE) - 1},
            id="in sentence, one code point short of its end",
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": AN_EMOJI_SENTENCE, "heard_to": 7},
            id="in an emoji sentence, the code point before its end",
        ),
    ],
)
def test_a_hold_place_that_keeps_its_invariants_reads_back_from_its_json_unchanged(
    fields: dict[str, Any],
) -> None:
    place = HoldPlace(**fields)

    assert HoldPlace.model_validate_json(place.model_dump_json()) == place
    assert json.loads(place.model_dump_json()).keys() == fields.keys()


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"at": HoldPoint.IN_SENTENCE}, id="in sentence without a sentence"),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": ""}, id="in sentence with an empty sentence"
        ),
        pytest.param(
            {"at": HoldPoint.BETWEEN_SENTENCES, "sentence": A_SENTENCE},
            id="between with a sentence",
        ),
        pytest.param(
            {"at": HoldPoint.BETWEEN_SENTENCES, "heard_to": 0}, id="between with heard_to"
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": A_SENTENCE, "heard_to": len(A_SENTENCE)},
            id="heard_to at the sentence's length",
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": A_SENTENCE, "heard_to": len(A_SENTENCE) + 1},
            id="heard_to beyond the sentence",
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": A_SENTENCE, "heard_to": -1},
            id="heard_to below zero",
        ),
        pytest.param(
            {"at": HoldPoint.IN_SENTENCE, "sentence": AN_EMOJI_SENTENCE, "heard_to": 8},
            id="heard_to at the length of an emoji sentence in code points",
        ),
    ],
)
def test_a_hold_place_that_breaks_an_invariant_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        HoldPlace(**fields)


@pytest.mark.parametrize(
    ("sentence", "heard_to", "expected"),
    [
        pytest.param(None, None, HoldPlace(at=HoldPoint.BETWEEN_SENTENCES), id="nothing plays"),
        pytest.param(
            A_SENTENCE,
            len(A_SENTENCE),
            HoldPlace(at=HoldPoint.BETWEEN_SENTENCES),
            id="the sentence played to its end",
        ),
        pytest.param(
            A_SENTENCE,
            5,
            HoldPlace(at=HoldPoint.IN_SENTENCE, sentence=A_SENTENCE, heard_to=5),
            id="the sentence stopped part way",
        ),
        pytest.param(
            A_SENTENCE,
            0,
            HoldPlace(at=HoldPoint.IN_SENTENCE, sentence=A_SENTENCE, heard_to=0),
            id="no word was heard",
        ),
        pytest.param(
            A_SENTENCE,
            None,
            HoldPlace(at=HoldPoint.IN_SENTENCE, sentence=A_SENTENCE),
            id="the app cannot tell",
        ),
    ],
)
def test_the_hold_place_maps_what_played_and_what_was_heard_of_it(
    sentence: str | None, heard_to: int | None, expected: HoldPlace
) -> None:
    assert hold_place(sentence, heard_to) == expected


@pytest.mark.parametrize("heard_to", [-1, len(A_SENTENCE) + 1], ids=["below zero", "beyond"])
def test_the_hold_place_fails_loud_for_a_heard_to_outside_the_sentence(heard_to: int) -> None:
    with pytest.raises(ValueError, match="heard_to"):
        hold_place(A_SENTENCE, heard_to)


@pytest.mark.parametrize("heard_to", [0, 1])
def test_the_hold_place_fails_loud_for_a_heard_to_without_a_sentence(heard_to: int) -> None:
    with pytest.raises(ValueError, match="heard_to"):
        hold_place(None, heard_to)


def test_a_window_reads_the_place_of_its_hold_back_from_its_json_unchanged() -> None:
    place = {"at": "in_sentence", "sentence": A_SENTENCE, "heard_to": 5}
    window = window_from(talked_window_wire(interrupted=interrupted_wire(place=place)))

    assert window.interrupted == Interrupted(
        id="h-1", place=HoldPlace(at=HoldPoint.IN_SENTENCE, sentence=A_SENTENCE, heard_to=5)
    )
    assert json.loads(window.model_dump_json())["interrupted"] == interrupted_wire(place=place)


def test_a_hold_without_a_place_dumps_without_the_key() -> None:
    assert Interrupted(id="h-1").place is None
    assert Interrupted(id="h-1").model_dump_json() == json.dumps(
        interrupted_wire(), separators=(",", ":")
    )


def test_a_window_without_a_hold_dumps_without_the_key() -> None:
    window = PlaceWindow(generation="g", starts_at=1, entries=())

    assert window.interrupted is None
    assert "interrupted" not in json.loads(window.model_dump_json())


@pytest.mark.parametrize(
    "interrupted",
    [{}, {"id": ""}, {"id": 1}, {"id": "h", "extra": 1}, "h-1"],
    ids=["no-id", "empty-id", "number-id", "unknown-member", "bare-id"],
)
def test_a_window_with_a_malformed_hold_is_refused(interrupted: object) -> None:
    with pytest.raises(ValidationError):
        window_from(talked_window_wire(interrupted=interrupted))


def test_a_window_without_talked_over_dumps_without_the_key() -> None:
    window = PlaceWindow(generation="g", starts_at=1, entries=())

    assert window.talked_over is None
    assert "talked_over" not in json.loads(window.model_dump_json())


def test_a_talked_over_window_reads_back_from_its_json_unchanged() -> None:
    window = window_from(
        talked_window_wire(
            talked_over=talked_over_wire(
                playing=False,
                speech=[{"name": "ada", "text": ""}, {"name": "bob", "text": "no, stop"}],
            )
        )
    )

    assert window.talked_over is not None
    assert [speech.name for speech in window.talked_over.speech] == ["ada", "bob"]
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


@pytest.mark.parametrize(
    "fields",
    [
        {"id": ""},
        {"playing": "yes"},
        {"speech": []},
        {"speech": [{"name": "", "text": "x"}]},
        {"speech": [{"name": "ada"}]},
        {"speech": [{"name": "ada", "text": "x", "extra": 1}]},
        {"extra": 1},
    ],
    ids=[
        "empty-id",
        "text-playing",
        "nobody-speaking",
        "empty-name",
        "no-text",
        "unknown-partial-member",
        "unknown-member",
    ],
)
def test_a_malformed_talked_over_record_is_refused(fields: dict[str, Any]) -> None:
    wire = talked_window_wire(talked_over=talked_over_wire(**fields))

    with pytest.raises(ValidationError):
        window_from(wire)


def test_a_talked_over_record_without_an_id_is_refused() -> None:
    wire = talked_over_wire()
    del wire["id"]

    with pytest.raises(ValidationError):
        TalkedOver.model_validate_json(json.dumps(wire))


def test_a_partial_is_no_entry_and_the_seq_rules_hold_beside_it() -> None:
    window = window_from(talked_window_wire(entries=[entry(3), entry(5)], starts_at=3))

    assert [line.seq for line in window.entries] == [3, 5]
    out_of_order = talked_window_wire(entries=[entry(2), entry(1)], starts_at=2)
    with pytest.raises(ValidationError):
        window_from(out_of_order)


@pytest.mark.parametrize(
    ("wire", "record"),
    [(spoken_wire(), Spoken), (written_wire(), Written)],
    ids=["spoken", "written"],
)
def test_a_speak_answer_reads_back_from_its_json_as_the_marker_alone(
    wire: dict[str, Any], record: type[Spoken | Written]
) -> None:
    answer = record.model_validate_json(json.dumps(wire))

    assert json.loads(answer.model_dump_json()) == wire


@pytest.mark.parametrize("record", [Spoken, Written])
@pytest.mark.parametrize(
    "marker",
    [{"false": False}, {"text": "yes"}, {"number": 1}, {"empty": None}],
    ids=lambda marker: next(iter(marker)),
)
def test_a_speak_answer_whose_marker_is_not_exactly_true_is_refused(
    record: type[Spoken | Written], marker: dict[str, Any]
) -> None:
    wire = {record.__name__.lower(): next(iter(marker.values()))}

    with pytest.raises(ValidationError):
        record.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("record", "wire"),
    [
        (Spoken, spoken_wire(extra=1)),
        (Spoken, {"duration_ms": 1200}),
        (Spoken, spoken_wire(duration_ms=1200)),
        (Spoken, {}),
        (Written, written_wire(extra=1)),
        (Written, {}),
    ],
    ids=[
        "spoken-extra",
        "spoken-length-only",
        "spoken-with-length",
        "spoken-empty",
        "written-extra",
        "written-empty",
    ],
)
def test_a_speak_answer_with_an_unknown_or_missing_field_is_refused(
    record: type[Spoken | Written], wire: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        record.model_validate_json(json.dumps(wire))


@pytest.mark.parametrize(
    ("wire", "record"),
    [(spoken_wire(), Spoken), (written_wire(), Written)],
    ids=["spoken", "written"],
)
def test_a_speak_answer_reads_spoken_and_written_each_as_its_own_record(
    wire: dict[str, Any], record: type[Spoken | Written]
) -> None:
    answer = TypeAdapter(SpeakAnswer).validate_json(json.dumps(wire))

    assert type(answer) is record


@pytest.mark.parametrize(
    "wire",
    [spoken_wire() | written_wire(), {}],
    ids=["both", "neither"],
)
def test_a_speak_answer_that_is_both_or_neither_is_refused(wire: dict[str, Any]) -> None:
    text = json.dumps(wire)
    adapter = TypeAdapter(SpeakAnswer)

    with pytest.raises(ValidationError):
        adapter.validate_json(text)


@pytest.mark.parametrize("state", [VoiceState.AVAILABLE, VoiceState.UNAVAILABLE])
def test_a_window_reads_its_voice_state_as_sent(state: VoiceState) -> None:
    window = window_from(talked_window_wire(voice_state=state.value))

    assert window.voice_state is state
    assert json.loads(window.model_dump_json())["voice_state"] == state.value
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


def test_a_window_without_a_voice_state_dumps_without_the_key() -> None:
    window = PlaceWindow(generation="g", starts_at=1, entries=())

    assert window.voice_state is None
    assert "voice_state" not in json.loads(window.model_dump_json())


@pytest.mark.parametrize("state", ["down", True, ""], ids=["unknown-word", "bool", "empty"])
def test_a_window_with_a_malformed_voice_state_is_refused(state: object) -> None:
    wire = talked_window_wire(voice_state=state)

    with pytest.raises(ValidationError):
        window_from(wire)


def test_a_cut_spoken_line_reads_back_from_its_json_with_its_mark() -> None:
    window = window_from(
        talked_window_wire(entries=[entry(1, own=True, spoken=True, cut=True, text="Shall we")])
    )

    assert window.entries[0].cut is True
    assert PlaceWindow.model_validate_json(window.model_dump_json()) == window


def test_a_line_that_is_not_cut_dumps_without_the_mark_and_reads_it_as_absent() -> None:
    window = window_from(talked_window_wire(entries=[entry(1, spoken=True)]))

    assert window.entries[0].cut is False
    assert "cut" not in json.loads(window.model_dump_json())["entries"][0]


def test_a_cut_mark_on_a_written_line_is_refused() -> None:
    wire = talked_window_wire(entries=[entry(1, spoken=False, cut=True)])

    with pytest.raises(ValidationError):
        window_from(wire)


@pytest.mark.parametrize("cut", ["yes", 1], ids=["text", "number"])
def test_an_entry_with_a_malformed_cut_mark_is_refused(cut: object) -> None:
    wire = talked_window_wire(entries=[entry(1, spoken=True, cut=cut)])

    with pytest.raises(ValidationError):
        window_from(wire)
