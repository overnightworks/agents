"""The floor of one agent at one place: who talks over its answer, whether it is held, and the
order of the sentences the answer says.

A talk-over holds the answer. A consumer keeps one ``Floor`` per agent and place in memory and calls
its methods as the events happen; the module reads no clock, runs no task and does no I/O, and
sound never reaches it. Two consumers drive it: the voice loop, which gives the partial speech of
the place as snapshots and holds its own answer, and an app's member wire, which gives each person's
events and answers a runner's ``speak``, ``stop_speaking`` and ``continue_speaking``. A call that
decides what a sentence does returns a step the consumer carries out: ``Start``, ``PlayOn``,
``Wait`` or ``Drop``; one that decides an answer to a runner returns it.

The floor orders the sentences the consumer enters and hands back what the consumer enters, never
reading it: the consumer's sentence stays its own text, sound and record. It works by position, so
two equal sentences stay two. A sentence is waiting once entered, playing from its start until the
consumer reports that it ended, and gone once dropped. A sentence entered between
``decision_opens`` and ``end_hold`` is the reply to the hold: it waits apart from the held answer's
sentences and takes their place when the hold ends with ``ANSWERED``. A render in flight is a
``RenderTicket`` the consumer ends, however its render returned; it counts as waiting.

A floor is in one of four phases:

- Silent: nothing plays, waits or renders, and no hold stands.
- Speaking: a sentence plays, waits or renders, or the loop's answer is open; no hold.
- Stopping: a hold arose and the consumer has not yet said where the sound stopped.
- Held: the place is known; with the app's id once the consumer gave it. The hold is wordless
  until a word of its talk-over is heard.

The span is the time during which a person's voice can talk over the member. It opens when a render
begins while none is open, or with the loop's answer, and is judged closed at an utterance's begin,
a lasting voice, a stop or a render's begin once nothing plays, waits or renders and no hold stands;
a stop always closes it. At its closing no utterance is a talk-over any more. A hold arises when a
voice whose turn began during the span lasts the bound. A key first heard through ``hear_partial``
with no begin before it began before: it holds nothing. A wordless hold may go on by itself once
nobody speaks. What a person who is still speaking does when a hold ends is the construction's
``AfterAnEnding``.

The talk-over: a person's utterance runs from its begin to its end; one that began during the span
is part of a talk-over, one that began before never is, however long it goes on. The consumer gives
a person's partial only once their voice has lasted its bound, so a shorter voice never reaches
this module. Whether a talk-over held a word is judged by person: from their first partial until
``forget_words``, a partial or a finished line of a person of the talk-over that holds a word
(``has_words``) counts, also after their partial went.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from agents.member_speaking import (
    ContinueSpeakingRefusal,
    HoldPlace,
    PartialSpeech,
    SpeakRefusal,
    StopSpeakingRefusal,
    has_words,
    hold_place,
)

__all__ = [
    "AfterAnEnding",
    "Drop",
    "Floor",
    "HoldEnding",
    "HoldStep",
    "PlayOn",
    "RenderTicket",
    "Start",
    "Wait",
    "cut_line",
]


class HoldEnding(StrEnum):
    """How a hold ends: the answer goes on, finishes its sentence, stops, is replaced or dropped,
    or goes on by itself because the talk-over held no word.

    The first five are what a hold decision gave; ``WENT_ON`` is no choice of the agent.
    """

    CONTINUED = "continued"
    FINISHED = "finished"
    STOPPED = "stopped"
    ANSWERED = "answered"
    DROPPED = "dropped"
    WENT_ON = "went_on"


class AfterAnEnding(StrEnum):
    """What a person who is still speaking is when a hold ends: ``HOLDS_AGAIN``, part of the
    standing talk-over, which holds the answer on or again; ``BEGAN_BEFORE``, a person whose turn
    began before the ending, who holds nothing."""

    HOLDS_AGAIN = "holds_again"
    BEGAN_BEFORE = "began_before"


_PLAYING_ENDINGS: Final = frozenset(
    {HoldEnding.CONTINUED, HoldEnding.FINISHED, HoldEnding.ANSWERED}
)

_DROPPING_ENDINGS: Final = frozenset(
    {HoldEnding.FINISHED, HoldEnding.ANSWERED, HoldEnding.STOPPED, HoldEnding.DROPPED}
)
"""Endings after which the held answer's waiting sentences and open renders are gone."""

_DROPPING_THE_HELD: Final = frozenset({HoldEnding.ANSWERED, HoldEnding.STOPPED, HoldEnding.DROPPED})
"""Endings that drop the sentence held too, instead of letting it play on."""

_NOTHING_PLAYS: Final = hold_place(None, None)


def cut_line(place: HoldPlace) -> str | None:
    """The line a sentence cut at ``place`` yields: its heard part, the whole sentence when the app
    cannot tell how far it played, none when no seat heard a word or no sentence was playing."""
    if place.sentence is None or place.heard_to == 0:
        return None
    return place.sentence if place.heard_to is None else place.sentence[: place.heard_to]


@dataclass(frozen=True)
class Start[Said]:
    """This sentence starts now: it has left the waiting ones and is the one that plays."""

    sentence: Said


@dataclass(frozen=True)
class PlayOn[Said]:
    """The held sentence plays on from where its sound stopped, at ``Floor.place``."""

    sentence: Said


@dataclass(frozen=True)
class Wait:
    """Nothing of the answer starts or plays on now, because a hold stands."""


@dataclass(frozen=True)
class Drop[Said]:
    """What a stop or an ending drops: the started sentence, if any, which the consumer stops and
    which leaves ``Floor.playing`` once the consumer reports that it ended, every waiting sentence,
    and ``cut``, the line the started sentence yields, set only when it names one and some seat
    heard a word of it."""

    sentence: Said | None
    waiting: tuple[Said, ...]
    cut: str | None


type HoldStep[Said] = PlayOn[Said] | Wait | Drop[Said]


class RenderTicket:
    """A sentence's render in flight, which counts as waiting until the consumer ends it."""


class TalkOver:
    """The span, the utterances at a place and which of them talk over the member."""

    def __init__(self) -> None:
        self._span_open = False
        self._began_in_span: dict[str, bool] = {}
        self._speaking: set[str] = set()
        self._talkers: set[str] = set()
        self._worded = False

    @property
    def span_open(self) -> bool:
        return self._span_open

    def span_opens(self) -> None:
        """A span opens: every utterance so far began before it."""
        self._span_open = True
        self._forget_what_began_in_the_span()

    def span_closes(self) -> None:
        """A span closes: no utterance is a talk-over any more, whatever it says next."""
        self._span_open = False
        self._forget_what_began_in_the_span()

    def _forget_what_began_in_the_span(self) -> None:
        self.everyone_began_before()
        self.forget_words()

    def everyone_began_before(self) -> None:
        """Every utterance that stands counts as begun before, so it holds nothing."""
        self._began_in_span = dict.fromkeys(self._began_in_span, False)

    def forget_words(self) -> None:
        """Count words afresh: the people of the talk-over are those who stand now."""
        self._talkers = {key for key, began in self._began_in_span.items() if began}
        self._worded = False

    def utterance_began(self, key: str) -> None:
        self._began_in_span[key] = self._span_open
        self._speaking.add(key)

    def utterance_ended(self, key: str) -> None:
        self._began_in_span.pop(key, None)
        self._speaking.discard(key)

    def in_utterance(self, key: str) -> bool:
        """The key's utterance began and neither its line nor its end came since."""
        return key in self._began_in_span

    def partial(self, key: str, text: str) -> bool:
        """``key``'s voice lasted the bound; whether its utterance began during the span."""
        if key not in self._began_in_span:
            self.utterance_began(key)
            self._began_in_span[key] = False
        began_in_span = self._began_in_span[key]
        if began_in_span:
            self._talkers.add(key)
            self._worded = self._worded or has_words(text)
        return began_in_span

    def finished(self, key: str, text: str) -> None:
        """A finished line of ``key`` ends their utterance as a talk-over; its words count when
        ``key`` is a person of the talk-over."""
        self._began_in_span.pop(key, None)
        self._worded = self._worded or (key in self._talkers and has_words(text))

    @property
    def speaking(self) -> frozenset[str]:
        return frozenset(self._speaking)

    @property
    def standing(self) -> bool:
        """Some utterance that began during the span has not ended."""
        return any(self._began_in_span.values())

    @property
    def worded(self) -> bool:
        """A partial or a line of a person of the talk-over held a word since ``forget_words``."""
        return self._worded

    @property
    def nobody_speaks(self) -> bool:
        """No person is speaking, whenever their utterance began."""
        return not self._speaking


@dataclass
class _Hold:
    """A hold: ``placed`` once the sound has stopped, then ``id`` when the app gave one."""

    placed: bool = False
    id: str | None = None


class Floor[Said]:
    """The talk-over at a place, the hold it puts on the answer and the order of its sentences."""

    def __init__(self, *, after_an_ending: AfterAnEnding) -> None:
        self._after_an_ending = after_an_ending
        self._talk = TalkOver()
        self._answer_open = False
        self._tickets: set[RenderTicket] = set()
        self._hold: _Hold | None = None
        self._wordless = False
        self._went_on: str | None = None
        self._place = _NOTHING_PLAYS
        self._waiting: list[Said] = []
        self._reply: list[Said] = []
        self._playing: Said | None = None
        self._deciding = False

    def answer_begins(self) -> None:
        """The loop's answer opens: no sentence waits, and every utterance so far began before."""
        self._waiting.clear()
        self._reply.clear()
        self._deciding = False
        self._answer_open = True
        self._talk.span_opens()

    def answer_ends(self) -> None:
        """The loop's answer has ended, and so has its hold."""
        self._answer_open = False
        self._hold = None
        self._talk.span_closes()

    def render_begins(self) -> RenderTicket:
        """A sentence's render begins; the consumer ends the ticket however the render returns."""
        self._close_the_span_once_silent()
        if not self._talk.span_open:
            self._talk.span_opens()
        ticket = RenderTicket()
        self._tickets.add(ticket)
        return ticket

    def render_ended(
        self, ticket: RenderTicket, interrupted: str | None = None
    ) -> SpeakRefusal | None:
        """A render ended: ``STOPPED`` when a stop or an answer dropped the ticket meanwhile, then,
        for a named hold, ``NOT_INTERRUPTED`` when it no longer stands. Otherwise the consumer
        calls ``end_hold(ANSWERED)`` and ``answer_goes_on`` when it named one, then ``enter``, or
        enters nothing."""
        if ticket not in self._tickets:
            return SpeakRefusal.STOPPED
        self._tickets.remove(ticket)
        if interrupted is not None and not self._stands(interrupted):
            return SpeakRefusal.NOT_INTERRUPTED
        return None

    def _close_the_span_once_silent(self) -> None:
        """Close the span of a render once nothing plays, waits or renders and no hold stands."""
        if self._talk.span_open and not self._answer_open and self._silent:
            self._talk.span_closes()

    def utterance_began(self, key: str) -> None:
        """A person's turn begins; it is part of a talk-over if the span is open now."""
        self._close_the_span_once_silent()
        self._talk.utterance_began(key)

    def utterance_ended(self, key: str) -> None:
        """A person's turn ends, whether it held a word or not."""
        self._talk.utterance_ended(key)

    def hear_partial(self, key: str, text: str) -> bool:
        """A person's voice lasted the bound; whether a hold arises with it."""
        self._close_the_span_once_silent()
        began_in_span = self._talk.partial(key, text)
        self._wordless = self._wordless and not self._talk.worded
        if not began_in_span or self._hold is not None:
            return False
        self._hold = _Hold()
        self._begin_stopping(self._hold)
        return True

    def hear_partials(self, speech: Sequence[PartialSpeech]) -> bool:
        """The partial speech now, whose people the loop's snapshot lists: whoever is not in it has
        ended their turn; a name with no open utterance begins one. Whether a hold arises."""
        listed = {partial.name for partial in speech}
        for key in self._talk.speaking - listed:
            self.utterance_ended(key)
        arises = False
        for partial in speech:
            if not self._talk.in_utterance(partial.name):
                self.utterance_began(partial.name)
            arises = self.hear_partial(partial.name, partial.text) or arises
        return arises

    def _begin_stopping(self, hold: _Hold) -> None:
        """The hold is Stopping while a sentence plays, and Held at once when none does."""
        hold.placed = self._playing is None
        if hold.placed:
            self._place = _NOTHING_PLAYS
            self._wordless = not self._talk.worded

    def hear_finished(self, key: str, text: str) -> None:
        """A person's finished line."""
        self._talk.finished(key, text)
        self._wordless = self._wordless and not self._talk.worded

    def enter(self, sentence: Said) -> None:
        """A sentence is written and joins the waiting ones, or the reply while a hold decides."""
        (self._reply if self._deciding else self._waiting).append(sentence)

    def next_start(self) -> Start[Said] | Wait | None:
        """The next sentence to start: ``Start``, ``Wait`` while a hold stands and a sentence
        waits, or nothing when none waits."""
        if not self._waiting:
            return None
        if self._hold is not None:
            return Wait()
        self._playing = self._waiting.pop(0)
        return Start(self._playing)

    def sentence_ended(self) -> None:
        """The sentence that started ended: played out, refused or stopped."""
        self._playing = None

    def sound_stopped_at(self, place: HoldPlace, hold_id: str | None = None) -> None:
        """Where the sound stopped, and the app's id of the hold if it gives one. A report is taken
        while Stopping, or while Held before an id stands; any other changes nothing."""
        hold = self._hold
        if hold is None:
            return
        if not hold.placed:
            hold.placed = True
            hold.id = hold_id
            self._place = place
            self._wordless = not self._talk.worded
        elif hold.id is None:
            hold.id = hold_id

    def decision_opens(self) -> None:
        """A hold decision opens: the words are counted afresh, and what is written from now on is
        the reply."""
        self._talk.forget_words()
        self._deciding = True

    def speak_refusal(self, interrupted: str | None) -> SpeakRefusal | None:
        """Why a ``speak`` naming ``interrupted`` is refused now, if it is: ``HELD`` while Held and
        no id is named, ``NOT_INTERRUPTED`` for any id that is not the standing one."""
        if interrupted is not None:
            return None if self._stands(interrupted) else SpeakRefusal.NOT_INTERRUPTED
        return SpeakRefusal.HELD if self._held else None

    def stop(self, interrupted: str | None, place: HoldPlace) -> Drop[Said] | StopSpeakingRefusal:
        """A stop: what it cuts and drops, or why it is refused. ``place`` is where the started
        sentence's sound was cut; the held place stands while Held. The member is Silent after it:
        every open ticket is dropped and the hold is over."""
        if interrupted is not None and not self._stands(interrupted):
            return StopSpeakingRefusal.NOT_INTERRUPTED
        if self._silent:
            return StopSpeakingRefusal.NOTHING_PLAYING
        dropped = self._dropped(HoldEnding.STOPPED, self._place if self._held else place)
        self._hold = None
        self._wordless = False
        self._talk.span_closes()
        return dropped

    def continue_speaking(
        self, interrupted: str
    ) -> tuple[HoldStep[Said], ...] | ContinueSpeakingRefusal:
        """A continue naming ``interrupted``: the steps of ``CONTINUED`` with the hold ended, or why
        it is refused."""
        if not self._stands(interrupted):
            return ContinueSpeakingRefusal.NOT_INTERRUPTED
        steps = self.end_hold(HoldEnding.CONTINUED)
        self.answer_goes_on()
        return steps

    def end_hold(self, ending: HoldEnding) -> tuple[HoldStep[Said], ...]:
        """A decision gave ``ending``, or a wordless hold may go on: what it does to the sentences,
        as the steps the consumer carries out in order, and none that is empty.

        ``Wait`` comes first when the hold stands on with the ending, the sound already stopped
        and, after ``ANSWERED``, between sentences. The hold itself ends only with
        ``answer_goes_on`` or the answer's end.
        """
        self._count_the_speaking_as_begun_before()
        holds_on = ending in _PLAYING_ENDINGS and self._talk.standing
        if ending is HoldEnding.WENT_ON and self._hold is not None and self._hold.id is not None:
            self._went_on = self._hold.id
        held = self._playing
        dropped = self._dropped(ending, self._place)
        if ending is HoldEnding.ANSWERED and holds_on:
            self._place = _NOTHING_PLAYS
        steps: list[HoldStep[Said]] = [Wait()] if holds_on else []
        if dropped.sentence is not None or dropped.waiting:
            steps.append(dropped)
        if held is not None and ending not in _DROPPING_THE_HELD and not holds_on:
            steps.append(PlayOn(held))
        return tuple(steps)

    def _dropped(self, ending: HoldEnding, place: HoldPlace) -> Drop[Said]:
        """What ``ending`` drops of the sentences and the open renders; the sentences the reply
        entered wait in their place after ``ANSWERED``. The cut line reads ``place``."""
        held = self._playing if ending in _DROPPING_THE_HELD else None
        dropping = ending in _DROPPING_ENDINGS
        dropped = Drop(
            held,
            tuple(self._waiting) if dropping else (),
            None if held is None else cut_line(place),
        )
        reply, self._reply = self._reply, []
        self._deciding = False
        if dropping:
            self._waiting = reply if ending is HoldEnding.ANSWERED else []
            self._tickets.clear()
        return dropped

    def answer_goes_on(self) -> bool:
        """The answer goes on after an ending or a wordless hold; whether a talk-over that stands
        holds it again at once. Otherwise the hold is over."""
        self._count_the_speaking_as_begun_before()
        if self._talk.standing and self._hold is not None:
            self._begin_stopping(self._hold)
            return True
        self._hold = None
        self._talk.forget_words()
        return False

    def _count_the_speaking_as_begun_before(self) -> None:
        if self._after_an_ending is AfterAnEnding.BEGAN_BEFORE:
            self._talk.everyone_began_before()

    def _stands(self, hold_id: str) -> bool:
        return self._held and self._hold is not None and self._hold.id == hold_id

    @property
    def _held(self) -> bool:
        return self._hold is not None and self._hold.placed

    @property
    def _silent(self) -> bool:
        return not (
            self._playing is not None
            or self._waiting
            or self._reply
            or self._tickets
            or self._hold is not None
        )

    @property
    def playing(self) -> Said | None:
        """The sentence that started and has not ended: the held one while a hold stands."""
        return self._playing

    @property
    def plays(self) -> bool:
        """A started sentence has not ended and the member is not Held."""
        return self._playing is not None and not self._held

    @property
    def waiting(self) -> tuple[Said, ...]:
        """The sentences written and not started, in written order."""
        return tuple(self._waiting)

    @property
    def renders(self) -> int:
        """The renders in flight: the open tickets."""
        return len(self._tickets)

    @property
    def holding(self) -> bool:
        return self._hold is not None

    @property
    def hold_id(self) -> str | None:
        """The app's id of the hold, once the consumer gave it."""
        return None if self._hold is None else self._hold.id

    @property
    def place(self) -> HoldPlace:
        """Where the sound stopped at the latest hold."""
        return self._place

    @property
    def standing(self) -> bool:
        """The talk-over still stands."""
        return self._talk.standing

    @property
    def worded(self) -> bool:
        """A word of the talk-over was heard since the words were counted afresh."""
        return self._talk.worded

    @property
    def wordless(self) -> bool:
        """Held, and no word of the talk-over was heard since the hold was placed."""
        return self._held and self._wordless

    @property
    def nobody_speaks(self) -> bool:
        """No person is speaking."""
        return self._talk.nobody_speaks

    @property
    def may_go_on(self) -> bool:
        """The hold is wordless and nobody speaks: it may go on by itself."""
        return self.wordless and self.nobody_speaks

    @property
    def went_on(self) -> str | None:
        """The id of the latest hold that went on by itself."""
        return self._went_on
