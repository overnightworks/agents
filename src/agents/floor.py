"""The floor of one agent at one place: who talks over its answer, whether it is held, and the
order of the sentences the answer says.

A talk-over holds the answer. The loop keeps one ``Floor`` per agent and place in memory and calls
its methods as the events happen; the module reads no clock, runs no task and does no I/O, and
sound never reaches it. ``hear_partials`` returns whether the consumer holds the answer; ``end_hold``
returns the steps the consumer carries out, ``Wait`` among them while the hold stands on.

The floor orders the sentences the consumer enters and hands back what the consumer enters, never
reading it: the consumer's sentence stays its own text, sound and record. It works by position, so
two equal sentences stay two. A call that decides what a sentence does returns a step the consumer
carries out: ``Start``, ``PlayOn``, ``Wait`` or ``Drop``. A sentence is waiting once entered,
playing from its start until the consumer reports that it ended, and gone once dropped. A sentence
entered between ``decision_opens`` and ``end_hold`` is the reply to the hold: it waits apart from
the held answer's sentences and takes their place when the hold ends with ``ANSWERED``.

A floor is in one of four phases, in the loop's meaning:

- Silent: no answer is open.
- Speaking: an answer is open, from ``answer_begins`` until it has ended, also while the reply is
  still written and nothing plays; no hold.
- Stopping: a hold arose and the loop has not yet said where the sound stopped.
- Held: the place is known. The hold is wordless until a word of its talk-over is heard.

A hold arises at the first partial of an utterance that began during the answer. A wordless hold may
go on by itself once nobody speaks. An ending that plays leaves the hold standing while a talk-over
still stands, at the same place, or between sentences after ``ANSWERED``. When the answer goes on, a
talk-over that stands holds it again at once; otherwise the hold is over and the words are counted
afresh. The answer's end ends the hold.

The talk-over: a person's utterance runs from the first partial that shows them to their finished
line or the first snapshot without them. One that began while the agent answered is part of a
talk-over; one that began before the answer never is, however long it goes on. The host gives a
person's partial only once their voice has lasted its bound, so a shorter voice never reaches this
module. Whether a talk-over held a word is judged by person: from their first partial until
``forget_words``, a partial or a finished line of a person of the talk-over that holds a word
(``has_words``) counts, also after their partial went.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from agents.member_speaking import HoldPlace, PartialSpeech, has_words, hold_place

__all__ = ["Drop", "Floor", "HoldEnding", "HoldStep", "PlayOn", "Start", "Wait"]


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


_PLAYING_ENDINGS: Final = frozenset(
    {HoldEnding.CONTINUED, HoldEnding.FINISHED, HoldEnding.ANSWERED}
)

_DROPPING_ENDINGS: Final = frozenset(
    {HoldEnding.FINISHED, HoldEnding.ANSWERED, HoldEnding.STOPPED, HoldEnding.DROPPED}
)
"""Endings after which the held answer's waiting sentences are gone."""

_DROPPING_THE_HELD: Final = frozenset({HoldEnding.ANSWERED, HoldEnding.STOPPED, HoldEnding.DROPPED})
"""Endings that drop the sentence held too, instead of letting it play on."""

_NOTHING_PLAYS: Final = hold_place(None, None)


@dataclass(frozen=True)
class Start[Sentence]:
    """This sentence starts now: it has left the waiting ones and is the one that plays."""

    sentence: Sentence


@dataclass(frozen=True)
class PlayOn[Sentence]:
    """The held sentence plays on from where its sound stopped, at ``Floor.place``."""

    sentence: Sentence


@dataclass(frozen=True)
class Wait:
    """Nothing of the answer starts or plays on now, because a hold stands."""


@dataclass(frozen=True)
class Drop[Sentence]:
    """A started sentence is dropped: the consumer stops its sound. It leaves ``Floor.playing``
    once the consumer reports that it ended."""

    sentence: Sentence


type HoldStep[Sentence] = PlayOn[Sentence] | Wait | Drop[Sentence]


class TalkOver:
    """The utterances at a place and which of them talk over an answer."""

    def __init__(self) -> None:
        self._answering = False
        self._began_during_answer: dict[str, bool] = {}
        self._speaking = False
        self._talkers: set[str] = set()
        self._worded = False

    def answer_begins(self) -> None:
        """An answer starts: every utterance so far began before it."""
        self._answering = True
        self._forget_what_began_during_the_answer()

    def answer_ends(self) -> None:
        """An answer ends: no utterance of it is a talk-over any more, whatever it says next."""
        self._answering = False
        self._forget_what_began_during_the_answer()

    def _forget_what_began_during_the_answer(self) -> None:
        self._began_during_answer = dict.fromkeys(self._began_during_answer, False)
        self.forget_words()

    def forget_words(self) -> None:
        """Count words afresh: the people of the talk-over are those who stand now."""
        self._talkers = {name for name, began in self._began_during_answer.items() if began}
        self._worded = False

    def partials(self, speech: Sequence[PartialSpeech]) -> None:
        """The partial speech at the place now: whoever is not in it has ended their utterance."""
        known = self._began_during_answer
        self._began_during_answer = {
            partial.name: known.get(partial.name, self._answering) for partial in speech
        }
        self._speaking = bool(speech)
        for partial in speech:
            if self._began_during_answer[partial.name]:
                self._talkers.add(partial.name)
                self._worded = self._worded or has_words(partial.text)

    def finished(self, name: str, text: str) -> None:
        """A finished line of ``name`` ends their utterance; its words count when ``name`` is a
        person of the talk-over."""
        self._began_during_answer.pop(name, None)
        self._worded = self._worded or (name in self._talkers and has_words(text))

    @property
    def standing(self) -> bool:
        """Some utterance that began during the answer has not ended."""
        return any(self._began_during_answer.values())

    @property
    def worded(self) -> bool:
        """A partial or a line of a person of the talk-over held a word since ``forget_words``."""
        return self._worded

    @property
    def nobody_speaks(self) -> bool:
        """The newest partial speech lists nobody, whenever their utterance began."""
        return not self._speaking


class Floor[Sentence]:
    """The talk-over at a place, the hold it puts on the answer and the order of its sentences."""

    def __init__(self) -> None:
        self._talk = TalkOver()
        self._holding = False
        self._place = _NOTHING_PLAYS
        self._waiting: list[Sentence] = []
        self._reply: list[Sentence] = []
        self._playing: Sentence | None = None
        self._deciding = False

    def answer_begins(self) -> None:
        """An answer opens: no sentence waits, and every utterance so far began before it."""
        self._waiting.clear()
        self._reply.clear()
        self._deciding = False
        self._talk.answer_begins()

    def answer_ends(self) -> None:
        """The answer has ended, and so has its hold."""
        self._holding = False
        self._talk.answer_ends()

    def hear_partials(self, speech: Sequence[PartialSpeech]) -> bool:
        """The partial speech now; whether a hold arises with it."""
        self._talk.partials(speech)
        if self._holding or not self._talk.standing:
            return False
        self._holding = True
        self._place_when_nothing_plays()
        return True

    def _place_when_nothing_plays(self) -> None:
        if self._playing is None:
            self._place = _NOTHING_PLAYS

    def hear_finished(self, name: str, text: str) -> None:
        """A person's finished line."""
        self._talk.finished(name, text)

    def enter(self, sentence: Sentence) -> None:
        """A sentence is written and joins the waiting ones, or the reply while a hold decides."""
        (self._reply if self._deciding else self._waiting).append(sentence)

    def next_start(self) -> Start[Sentence] | Wait | None:
        """The next sentence to start: ``Start``, ``Wait`` while a hold stands and a sentence
        waits, or nothing when none waits."""
        if not self._waiting:
            return None
        if self._holding:
            return Wait()
        self._playing = self._waiting.pop(0)
        return Start(self._playing)

    def sentence_ended(self) -> None:
        """The sentence that started ended: played out, refused or stopped."""
        self._playing = None

    def sound_stopped_at(self, place: HoldPlace) -> None:
        """Where the sound stopped."""
        self._place = place

    def decision_opens(self) -> None:
        """A hold decision opens: the words are counted afresh, and what is written from now on is
        the reply."""
        self._talk.forget_words()
        self._deciding = True

    def end_hold(self, ending: HoldEnding) -> tuple[HoldStep[Sentence], ...]:
        """A decision gave ``ending``, or a wordless hold may go on: what it does to the sentences,
        as the steps the consumer carries out in order.

        ``Wait`` comes first when the hold stands on with the ending, the sound already stopped
        and, after ``ANSWERED``, between sentences. The hold itself ends only with
        ``answer_goes_on`` or the answer's end.
        """
        holds_on = ending in _PLAYING_ENDINGS and self._talk.standing
        reply, self._reply = self._reply, []
        self._deciding = False
        if ending is HoldEnding.ANSWERED:
            self._waiting = reply
            if holds_on:
                self._place = _NOTHING_PLAYS
        elif ending in _DROPPING_ENDINGS:
            self._waiting = []
        steps: list[HoldStep[Sentence]] = [Wait()] if holds_on else []
        if (held := self._playing) is not None:
            if ending in _DROPPING_THE_HELD:
                steps.append(Drop(held))
            elif not holds_on:
                steps.append(PlayOn(held))
        return tuple(steps)

    def answer_goes_on(self) -> bool:
        """The answer goes on after an ending or a wordless hold; whether a talk-over that stands
        holds it again at once. Otherwise the hold is over."""
        if self._talk.standing:
            self._place_when_nothing_plays()
            return True
        self._holding = False
        self._talk.forget_words()
        return False

    @property
    def playing(self) -> Sentence | None:
        """The sentence that started and has not ended: the held one while a hold stands."""
        return self._playing

    @property
    def waiting(self) -> tuple[Sentence, ...]:
        """The sentences written and not started, in written order."""
        return tuple(self._waiting)

    @property
    def holding(self) -> bool:
        return self._holding

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
    def nobody_speaks(self) -> bool:
        """The newest partial speech lists nobody."""
        return self._talk.nobody_speaks
