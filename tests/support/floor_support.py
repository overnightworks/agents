"""Builders that bring an `agents.floor.Floor` into a state, through its events only."""

from agents import floor
from agents.floor import Floor, RenderTicket, Start
from agents.member_speaking import HoldPlace, PartialSpeech, hold_place

HOLD = "hold-1"
FIRST = "Eins zwei drei."
SECOND = "Vier fünf."
THIRD = "Sechs."
CUT_AT = len("Eins")


def wire_floor() -> Floor[str]:
    """The member wire's floor: every person speaking at an ending counts as begun before."""
    return Floor[str](after_an_ending=floor.AfterAnEnding.BEGAN_BEFORE)


def loop_floor() -> Floor[str]:
    """The voice loop's floor: a talk-over that stands at an ending holds on or again."""
    return Floor[str](after_an_ending=floor.AfterAnEnding.HOLDS_AGAIN)


def partial(name: str, text: str = "") -> PartialSpeech:
    return PartialSpeech(name=name, text=text)


def place_in(sentence: str = FIRST, heard_to: int | None = CUT_AT) -> HoldPlace:
    return hold_place(sentence, heard_to)


def rendered(wire: Floor[str], sentence: str) -> RenderTicket:
    """A voiced sentence of the wire: its render begins and ends, and it waits."""
    ticket = wire.render_begins()
    assert wire.render_ended(ticket) is None
    wire.enter(sentence)
    return ticket


def playing(wire: Floor[str], sentence: str = FIRST, *waiting: str) -> Floor[str]:
    """The wire's floor with ``sentence`` playing and ``waiting`` behind it."""
    rendered(wire, sentence)
    assert wire.next_start() == Start(sentence)
    for later in waiting:
        rendered(wire, later)
    return wire


def lasts(member: Floor[str], key: str, text: str = "") -> bool:
    """A person's turn begins and their voice lasts the bound; whether a hold arises."""
    member.utterance_began(key)
    return member.hear_partial(key, text)


def stopping(sentence: str = FIRST, *waiting: str) -> Floor[str]:
    """A sentence plays, and a person's lasting voice has begun its stop."""
    wire = playing(wire_floor(), sentence, *waiting)
    assert lasts(wire, "ann")
    return wire


def held(
    sentence: str = FIRST, *waiting: str, heard_to: int | None = CUT_AT, key: str = "ann"
) -> Floor[str]:
    """The sentence's sound stopped under the hold ``HOLD``, with ``waiting`` behind it."""
    wire = playing(wire_floor(), sentence, *waiting)
    assert lasts(wire, key, "Moment")
    wire.sound_stopped_at(hold_place(sentence, heard_to), HOLD)
    return wire


def held_at_once(*waiting: str, key: str = "ann", text: str = "Moment") -> Floor[str]:
    """Nothing plays and ``waiting`` waits as a lasting voice holds the member."""
    wire = wire_floor()
    for sentence in waiting:
        rendered(wire, sentence)
    assert lasts(wire, key, text)
    wire.sound_stopped_at(wire.place, HOLD)
    return wire
