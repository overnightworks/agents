"""A runner reads a resource record a newer app wrote; every other path stays strict."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

import pytest
from pydantic import Field, ValidationError

from agents.member import (
    Interrupted,
    MemberResourceRecord,
    MemberStanding,
    PlaceRef,
    PlaceView,
    PlaceWindow,
    Standing,
    StandingState,
    member_resource_from_json,
)
from agents.turn import StrictModel
from tests.support.member_wire import entry, talked_window_wire

PLACE = {"kind": "room", "id": "r-1"}
INVITATION = {"id": "i-1", "epoch": 3, "expires_at": "2026-11-08T12:00:00Z"}
PARENT = {"app": "https://app.example/mcp", "place": {"kind": "room", "id": "p-9"}}
STANDING = {"place": PLACE, "state": "invited", "invitation": INVITATION, "parent": PARENT}
STANDINGS = {"standings": [STANDING]}
WINDOW = talked_window_wire(interrupted={"id": "h-1"}, voice_state="available")
A_PLACE = {"at": "in_sentence", "sentence": "Guten Tag.", "heard_to": 5}
PLACED_WINDOW = talked_window_wire(interrupted={"id": "h-1", "place": A_PLACE})
VIEW = {"revision": 2, "awaits_you": True, "state": {"items": [1, {"open": True}]}}

# each record, its wire, and the places inside that wire where a newer app may add a key
UNKNOWN_KEY_PLACES = [
    pytest.param(MemberStanding, STANDINGS, (), id="standings"),
    pytest.param(MemberStanding, STANDINGS, ("standings", 0), id="standing"),
    pytest.param(MemberStanding, STANDINGS, ("standings", 0, "place"), id="place"),
    pytest.param(MemberStanding, STANDINGS, ("standings", 0, "invitation"), id="invitation"),
    pytest.param(MemberStanding, STANDINGS, ("standings", 0, "parent"), id="parent"),
    pytest.param(MemberStanding, STANDINGS, ("standings", 0, "parent", "place"), id="parent place"),
    pytest.param(PlaceWindow, WINDOW, (), id="window"),
    pytest.param(PlaceWindow, WINDOW, ("entries", 0), id="entry"),
    pytest.param(PlaceWindow, WINDOW, ("talked_over",), id="talked over"),
    pytest.param(PlaceWindow, WINDOW, ("talked_over", "speech", 0), id="partial speech"),
    pytest.param(PlaceWindow, WINDOW, ("interrupted",), id="interrupted"),
    pytest.param(PlaceWindow, PLACED_WINDOW, ("interrupted", "place"), id="hold place"),
    pytest.param(PlaceView, VIEW, (), id="view"),
]


def with_key_at(wire: dict[str, Any], path: tuple[str | int, ...]) -> str:
    copy: dict[str, Any] = json.loads(json.dumps(wire))
    holder: Any = copy
    for step in path:
        holder = holder[step]
    holder["added_later"] = {"any": ["json", 1]}
    return json.dumps(copy)


@pytest.mark.parametrize(("record", "wire", "path"), UNKNOWN_KEY_PLACES)
def test_a_resource_record_with_an_unknown_key_reads_as_without_it(
    record: type[MemberResourceRecord], wire: dict[str, Any], path: tuple[str | int, ...]
) -> None:
    read = member_resource_from_json(record, with_key_at(wire, path))

    assert read == member_resource_from_json(record, json.dumps(wire))
    assert "added_later" not in read.model_dump_json()


@pytest.mark.parametrize(("record", "wire", "path"), UNKNOWN_KEY_PLACES)
def test_the_default_parse_still_refuses_a_resource_record_with_an_unknown_key(
    record: type[MemberResourceRecord], wire: dict[str, Any], path: tuple[str | int, ...]
) -> None:
    payload = with_key_at(wire, path)

    with pytest.raises(ValidationError):
        record.model_validate_json(payload)


def mutated(wire: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {**wire, **fields}


@pytest.mark.parametrize(
    ("record", "wire"),
    [
        pytest.param(PlaceWindow, {"starts_at": 1, "entries": []}, id="a missing required field"),
        pytest.param(PlaceWindow, mutated(WINDOW, starts_at="1"), id="a retyped field"),
        pytest.param(PlaceWindow, mutated(WINDOW, voice_state="down"), id="an unknown enum value"),
        pytest.param(PlaceWindow, mutated(WINDOW, entries=[entry(0)]), id="a violated bound"),
        pytest.param(
            MemberStanding,
            {"standings": [{**STANDING, "state": "gone"}]},
            id="an unknown standing state",
        ),
        pytest.param(PlaceView, mutated(VIEW, awaits_you="yes"), id="a view's retyped field"),
        pytest.param(PlaceWindow, mutated(WINDOW, entries=[entry(1, cut=True)]), id="cut unspoken"),
        pytest.param(
            PlaceWindow, mutated(WINDOW, entries=[entry(1, language="de")]), id="language unspoken"
        ),
        pytest.param(
            PlaceWindow,
            mutated(WINDOW, starts_at=1, entries=[entry(1), entry(1)]),
            id="seq not ascending",
        ),
        pytest.param(
            MemberStanding,
            {"standings": [{**STANDING, "state": "active"}]},
            id="invitation outside invited",
        ),
    ],
)
def test_the_tolerant_reader_still_refuses_what_the_default_parse_refuses_besides_a_key(
    record: type[MemberResourceRecord], wire: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        member_resource_from_json(record, json.dumps({**wire, "added_later": 1}))


class OlderInterrupted(StrictModel):
    id: str


class WindowOfAnOlderRelease(PlaceWindow):
    interrupted: OlderInterrupted | None = None


class WindowWithoutWordlessHolds(StrictModel):
    generation: str
    starts_at: int
    entries: tuple[dict[str, Any], ...]
    interrupted: OlderInterrupted | None = None


def test_a_hold_without_a_place_reads_as_one_the_app_states_none_for() -> None:
    window = member_resource_from_json(PlaceWindow, json.dumps(WINDOW))

    assert window.interrupted == Interrupted(id="h-1")


def test_a_reader_of_an_older_release_ignores_what_a_hold_without_words_adds() -> None:
    wire = talked_window_wire(interrupted={"id": "h-1", "wordless": True}, went_on={"id": "h-0"})

    window = WindowWithoutWordlessHolds.model_validate_json(json.dumps(wire), extra="ignore")

    assert window.interrupted == OlderInterrupted(id="h-1")
    assert "went_on" not in window.model_dump_json()


def test_a_reader_of_an_older_release_ignores_the_place_of_a_hold() -> None:
    window = member_resource_from_json(WindowOfAnOlderRelease, json.dumps(PLACED_WINDOW))

    assert window.interrupted == OlderInterrupted(id="h-1")


class Circle(StrictModel):
    tag: Literal["circle"] = "circle"


class Square(StrictModel):
    tag: Literal["square"] = "square"


class ShapedWindow(PlaceWindow):
    shape: Annotated[Circle | Square, Field(discriminator="tag")]


def test_an_unknown_variant_of_a_tagged_union_is_refused_by_the_tolerant_reader() -> None:
    known = json.dumps(mutated(WINDOW, shape={"tag": "circle", "added_later": 1}))
    unknown = json.dumps(mutated(WINDOW, shape={"tag": "hexagon", "added_later": 1}))

    assert member_resource_from_json(ShapedWindow, known).shape == Circle()
    with pytest.raises(ValidationError):
        member_resource_from_json(ShapedWindow, unknown)


def test_a_views_state_keeps_an_unknown_key_of_the_apps_document() -> None:
    state = {"added_later": 1, "group": {"added_later": [None, 1.5]}}
    wire = json.dumps(mutated(VIEW, state=state))

    assert member_resource_from_json(PlaceView, wire).state == state


@pytest.mark.parametrize(
    ("record", "fields"),
    [
        pytest.param(PlaceWindow, {"generation": "g", "starts_at": 1, "entries": ()}, id="window"),
        pytest.param(PlaceView, {"revision": 1, "awaits_you": False, "state": {}}, id="view"),
        pytest.param(
            Standing,
            {"place": PlaceRef(kind="room", id="r-1"), "state": StandingState.ACTIVE},
            id="standing",
        ),
    ],
)
def test_a_record_built_with_an_unknown_field_is_refused(
    record: type[Any], fields: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        record(**fields, added_later=1)
