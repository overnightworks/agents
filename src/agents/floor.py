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
sentences, takes their place when the hold ends with ``ANSWERED`` and goes ahead of them when it
ends with ``SAID_THEN_CONTINUED``, until the consumer reports ``reply_ended``. A render in flight is
a ``RenderTicket`` the consumer ends, however its render returned; it counts as waiting.

A choice that plays something starts only once every person of the hold's talk-over has ended
their turn. ``end_hold`` and the calls that wrap it return ``Wait`` first and keep the choice
waiting; it plays and drops nothing until ``choice_may_play``, when the consumer takes its steps
with ``play_choice`` in the step of the event that made it true. A choice that carries the count
its decision was told (``told_changes``) is refused ``Stale`` when the floor counted more
``worded_changes`` since: nothing is applied, and the loop decides again. A waiting choice is
void, and a new hold stands at the same place, when a person who began after it arrived voices the
bound (their first partial), when a person speaking at it whose words changed does in a snapshot,
or when the consumer reports ``voice_went_on``.

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
nobody speaks.

The talk-over: a person's utterance runs from its begin to its end; one that began during the span
is part of a talk-over, one that began before never is, however long it goes on. The consumer gives
a person's partial only once their voice has lasted its bound, so a shorter voice never reaches
this module. The talk-over's people stay the hold's for its life. Whether a talk-over held a word
is judged by person: from their first partial until the hold ends, a partial or a finished line
of a person of the talk-over whose words (``words_of``) are not empty counts, also after their
partial went. ``worded_changes`` counts each partial or line whose words differ from what that
person was last heard to say.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from agents.member_speaking import (
    ContinueSpeakingRefusal,
    HoldPlace,
    PartialSpeech,
    SpeakRefusal,
    StopSpeakingRefusal,
    hold_place,
)
from agents.talk_over import TalkOver, ToldTalkOver

__all__ = [
    "Drop",
    "Floor",
    "HoldChoice",
    "HoldEnding",
    "HoldStep",
    "PlayOn",
    "RenderTicket",
    "Stale",
    "Start",
    "ToldTalkOver",
    "Wait",
    "cut_line",
    "leaves_the_turn",
    "replies",
]


class HoldEnding(StrEnum):
    """How a hold ends: the answer goes on, finishes its sentence, says a short line of its own
    and then goes on, stops, is replaced or dropped, or goes on by itself because the talk-over
    held no word.

    The first five of ``HoldChoice`` are what a hold decision gave; ``WENT_ON`` is no choice of
    the agent and ``DROPPED`` a decision that gave none, which the core carries out as ``STOPPED``.
    """

    CONTINUED = "continued"
    FINISHED = "finished"
    SAID_THEN_CONTINUED = "said_then_continued"
    STOPPED = "stopped"
    ANSWERED = "answered"
    DROPPED = "dropped"
    WENT_ON = "went_on"


type HoldChoice = Literal[
    HoldEnding.CONTINUED,
    HoldEnding.FINISHED,
    HoldEnding.SAID_THEN_CONTINUED,
    HoldEnding.ANSWERED,
    HoldEnding.STOPPED,
]
"""The agent's five ways back from a hold, the same in every app: go on at the place, finish
only the sentence and stop, a short own line and then go on, say something else instead, stop."""


class _Reply(StrEnum):
    """Where the sentences the reply entered go: ``NOWHERE``, ``INSTEAD`` of the waiting ones or
    ``AHEAD`` of them."""

    NOWHERE = "nowhere"
    INSTEAD = "instead"
    AHEAD = "ahead"


@dataclass(frozen=True)
class _Effect:
    """What an ending does to the sentences: ``plays`` something after it, so a talk-over that
    stands holds it on; ``drops_the_held`` sentence instead of letting it play on;
    ``drops_the_waiting`` sentences and the open renders; and where the ``reply`` goes."""

    plays: bool
    drops_the_held: bool
    drops_the_waiting: bool
    reply: _Reply


_EFFECTS: Final = {
    HoldEnding.CONTINUED: _Effect(
        plays=True, drops_the_held=False, drops_the_waiting=False, reply=_Reply.NOWHERE
    ),
    HoldEnding.FINISHED: _Effect(
        plays=True, drops_the_held=False, drops_the_waiting=True, reply=_Reply.NOWHERE
    ),
    HoldEnding.SAID_THEN_CONTINUED: _Effect(
        plays=True, drops_the_held=True, drops_the_waiting=False, reply=_Reply.AHEAD
    ),
    HoldEnding.ANSWERED: _Effect(
        plays=True, drops_the_held=True, drops_the_waiting=True, reply=_Reply.INSTEAD
    ),
    HoldEnding.STOPPED: _Effect(
        plays=False, drops_the_held=True, drops_the_waiting=True, reply=_Reply.NOWHERE
    ),
    HoldEnding.WENT_ON: _Effect(
        plays=False, drops_the_held=False, drops_the_waiting=False, reply=_Reply.NOWHERE
    ),
}
"""The one table of what each ending does to the sentences."""

_NOTHING_PLAYS: Final = hold_place(None, None)


def leaves_the_turn(choice: HoldChoice) -> bool:
    """Whether ``choice`` drops the held sentence, so the consumer stops the held answer's turn."""
    return _EFFECTS[choice].drops_the_held


def replies(choice: HoldChoice) -> bool:
    """Whether ``choice`` says the sentences of the hold decision's reply, so they play in the
    held answer's place."""
    return _EFFECTS[choice].reply is not _Reply.NOWHERE


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
    and ``cut``, the line the started sentence yields, set only when it names one, as
    ``cut_line(place)``."""

    sentence: Said | None
    waiting: tuple[Said, ...]
    cut: str | None


type HoldStep[Said] = PlayOn[Said] | Wait | Drop[Said]


@dataclass(frozen=True)
class Stale:
    """The choice was told fewer worded changes than the floor counts; nothing was applied."""


class RenderTicket:
    """A sentence's render in flight, which counts as waiting until the consumer ends it."""


@dataclass
class _Hold:
    """A hold: ``placed`` once the sound has stopped, then ``id`` when the app gave one."""

    placed: bool = False
    id: str | None = None


@dataclass
class _WaitingChoice:
    """A choice that plays and waits for the talk-over: ``speaking`` are the people speaking at its
    arrival, whose voice a later partial does not show to have begun after it."""

    choice: HoldChoice
    speaking: set[str]


class Floor[Said]:
    """The talk-over at a place, the hold it puts on the answer and the order of its sentences."""

    def __init__(self) -> None:
        self._talk = TalkOver()
        self._answer_open = False
        self._tickets: set[RenderTicket] = set()
        self._hold: _Hold | None = None
        self._choice: _WaitingChoice | None = None
        self._wordless = False
        self._went_on: str | None = None
        self._place = _NOTHING_PLAYS
        self._waiting: list[Said] = []
        self._reply: list[Said] = []
        self._behind: list[Said] = []
        self._playing: Said | None = None
        self._deciding = False

    def answer_begins(self) -> None:
        """The loop's answer opens: no sentence waits, and every utterance so far began before."""
        self._waiting.clear()
        self._reply.clear()
        self._behind.clear()
        self._deciding = False
        self._choice = None
        self._answer_open = True
        self._talk.span_opens()

    def answer_ends(self) -> None:
        """The loop's answer has ended, and so has its hold."""
        self._answer_open = False
        self._hold = None
        self._choice = None
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
        calls ``end_hold(ANSWERED)``, or ``end_hold(SAID_THEN_CONTINUED)`` when the sentence keeps
        the queue, and ``answer_goes_on`` when it named one and the choice plays, then ``enter``
        and, after the second, ``reply_ended``, or enters nothing."""
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
        if self._choice is not None:
            self._choice.speaking.discard(key)

    def hear_partial(self, key: str, text: str) -> bool:
        """A person's voice lasted the bound; whether a hold arises with it, which a waiting
        choice's void does too: a talk-over person whose utterance began after the choice arrived
        voices the bound with their first partial."""
        return self._hear_partial(key, text, in_a_snapshot=False)

    def hear_partials(self, speech: Sequence[PartialSpeech]) -> bool:
        """The partial speech now, whose people the loop's snapshot lists: whoever is not in it has
        ended their turn; a name with no open utterance begins one. Whether a hold arises, a
        waiting choice's void included, which a snapshot also shows by new words of a person
        speaking at the choice. The partials are judged before the ends."""
        arises = False
        for partial in speech:
            if not self._talk.in_utterance(partial.name):
                self.utterance_began(partial.name)
            arises = self._hear_partial(partial.name, partial.text, in_a_snapshot=True) or arises
        listed = {partial.name for partial in speech}
        for key in self._talk.speaking - listed:
            self.utterance_ended(key)
        return arises

    def _hear_partial(self, key: str, text: str, *, in_a_snapshot: bool) -> bool:
        self._close_the_span_once_silent()
        before = self._talk.worded_changes
        began_in_span = self._talk.partial(key, text)
        self._note_words()
        if not began_in_span:
            return False
        if self._choice is not None:
            new_voice = key not in self._choice.speaking
            went_on = in_a_snapshot and self._talk.worded_changes > before
            if new_voice or went_on:
                self._void_the_choice()
                return True
        if self._hold is not None:
            return False
        self._hold = _Hold()
        self._begin_stopping(self._hold)
        return True

    def voice_went_on(self, key: str) -> bool:
        """The voice of a person who was speaking at the waiting choice lasts the bound after it;
        whether that voids the choice, so a new hold stands. The consumer reports it after the
        person's first ``hear_partial``."""
        if (
            self._choice is None
            or key not in self._choice.speaking
            or not self._talk.is_talker(key)
        ):
            return False
        self._void_the_choice()
        return True

    def _void_the_choice(self) -> None:
        """The waiting choice plays and drops nothing and its reply's sentences go; a new hold
        stands at the same place, judged afresh, with no id until the consumer gives one."""
        self._choice = None
        self._reply.clear()
        self._deciding = False
        self._talk.forget_words()
        self._hold = _Hold()
        self._place_hold(self._hold, self._place)

    def _begin_stopping(self, hold: _Hold) -> None:
        """The hold is Stopping while a sentence plays, and Held at once when none does."""
        if self._playing is None:
            self._place_hold(hold, _NOTHING_PLAYS)

    def _place_hold(self, hold: _Hold, place: HoldPlace) -> None:
        """The sound stopped at ``place``: the hold is placed, wordless while no word was heard."""
        hold.placed = True
        self._place = place
        self._wordless = not self._talk.worded

    def _note_words(self) -> None:
        """A heard word ends a wordless hold."""
        self._wordless = self._wordless and not self._talk.worded

    def hear_finished(self, key: str, text: str) -> bool:
        """A person's finished line; whether it repeats the words a hold decision was told of them,
        which the decision has answered."""
        repeats = self._talk.finished(key, text)
        self._note_words()
        return repeats

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
            hold.id = hold_id
            self._place_hold(hold, place)
        elif hold.id is None:
            hold.id = hold_id

    def decision_opens(self) -> ToldTalkOver:
        """A hold decision opens: what is written from now on is the reply, and one left by an
        earlier decision is gone. It is told the count of worded changes and the partials that
        stand, and a choice of it carries that count as ``told_changes``."""
        self._reply.clear()
        self._deciding = True
        return self._talk.told()

    def speak_refusal(self, interrupted: str | None) -> SpeakRefusal | None:
        """Why a ``speak`` naming ``interrupted`` is refused now, if it is: ``HELD`` while Held and
        no id is named, ``NOT_INTERRUPTED`` for any id that is not the standing one."""
        if interrupted is not None:
            return None if self._stands(interrupted) else SpeakRefusal.NOT_INTERRUPTED
        return SpeakRefusal.HELD if self._held else None

    def stop(self, interrupted: str | None, place: HoldPlace) -> Drop[Said] | StopSpeakingRefusal:
        """A stop: what it cuts and drops, or why it is refused. ``place`` is where the started
        sentence's sound was cut; the held place stands while Held. The member is Silent after it:
        every open ticket is dropped, a waiting choice is void and the hold is over."""
        if interrupted is not None and not self._stands(interrupted):
            return StopSpeakingRefusal.NOT_INTERRUPTED
        if self._silent:
            return StopSpeakingRefusal.NOTHING_PLAYING
        dropped = self._dropped(_EFFECTS[HoldEnding.STOPPED], self._place if self._held else place)
        self._choice = None
        self._hold = None
        self._wordless = False
        self._talk.span_closes()
        return dropped

    def continue_speaking(
        self, interrupted: str, *, finish: bool = False
    ) -> tuple[HoldStep[Said], ...] | ContinueSpeakingRefusal:
        """A continue naming ``interrupted``: the steps of ``CONTINUED``, or of ``FINISHED`` when
        ``finish`` is set, with the hold ended, ``Wait`` while the choice waits for the talk-over,
        or why it is refused."""
        if not self._stands(interrupted):
            return ContinueSpeakingRefusal.NOT_INTERRUPTED
        steps = self._ending_of(HoldEnding.FINISHED if finish else HoldEnding.CONTINUED)
        if not self.choice_waits:
            self.answer_goes_on()
        return steps

    def end_hold(
        self, choice: HoldChoice, *, told_changes: int | None = None
    ) -> tuple[HoldStep[Said], ...] | Stale:
        """A decision gave ``choice``: what it does to the sentences, as the steps the consumer
        carries out in order, and none that is empty. ``Stale`` changes nothing when
        ``told_changes`` is below ``worded_changes``; ``None`` means the decision was not told.

        A choice that plays something while a person of the talk-over speaks waits: the answer is
        ``(Wait(),)``, nothing is applied and the sentences entered meanwhile are its reply. A
        later choice that plays replaces the waiting one, whose reply's sentences go; a stopping
        one acts at once and voids it. The consumer takes a waiting choice's steps with
        ``play_choice``. The hold itself ends only with ``answer_goes_on`` or the answer's end.
        """
        if self._is_stale(told_changes):
            return Stale()
        return self._ending_of(choice)

    def _ending_of(self, choice: HoldChoice) -> tuple[HoldStep[Said], ...]:
        if not _EFFECTS[choice].plays:
            return self._ending(choice)
        if self._choice is not None:
            self._reply.clear()
        if self._talk.talk_over_speaks:
            self._choice = _WaitingChoice(choice, set(self._talk.speaking))
            self._deciding = True  # the wire's speak enters its sentence after this call
            return (Wait(),)
        return self._ending(choice)

    def play_choice(self) -> tuple[HoldStep[Said], ...]:
        """The steps of the waiting choice, once ``choice_may_play``; the consumer carries them out
        and then calls ``answer_goes_on``."""
        if self._choice is None or self._talk.talk_over_speaks:
            raise ValueError("no waiting choice may play")
        return self._ending(self._choice.choice)

    def go_on(self) -> tuple[HoldStep[Said], ...]:
        """A wordless hold goes on by itself, as ``CONTINUED`` would: the steps of ``WENT_ON``,
        named by ``went_on`` when the hold has an id."""
        if self._hold is not None and self._hold.id is not None:
            self._went_on = self._hold.id
        return self._ending(HoldEnding.WENT_ON)

    def reply_ended(self) -> None:
        """The reply that went ahead of the waiting sentences has ended: they follow it now."""
        self._waiting.extend(self._behind)
        self._behind.clear()

    def _ending(self, ending: HoldEnding) -> tuple[HoldStep[Said], ...]:
        self._choice = None
        effect = _EFFECTS[ending]
        held = self._playing
        dropped = self._dropped(effect, self._place)
        steps: list[HoldStep[Said]] = []
        if dropped.sentence is not None or dropped.waiting:
            steps.append(dropped)
        if held is not None and not effect.drops_the_held:
            steps.append(PlayOn(held))
        return tuple(steps)

    def _dropped(self, effect: _Effect, place: HoldPlace) -> Drop[Said]:
        """What ``effect`` drops of the sentences and the open renders, and where the sentences the
        reply entered go. The cut line reads ``place``."""
        held = self._playing if effect.drops_the_held else None
        reply, self._reply = self._reply, []
        self._deciding = False
        dropping = (
            (*self._waiting, *self._behind, *(reply if effect.reply is _Reply.NOWHERE else ()))
            if effect.drops_the_waiting
            else ()
        )
        dropped = Drop(held, dropping, None if held is None else cut_line(place))
        if effect.drops_the_waiting:
            self._waiting = reply if effect.reply is _Reply.INSTEAD else []
            self._behind.clear()
            self._tickets.clear()
        elif effect.reply is _Reply.AHEAD:
            self._behind = [*self._waiting, *self._behind]
            self._waiting = reply
        return dropped

    def answer_goes_on(self) -> bool:
        """The answer goes on after an ending or a wordless hold; whether a person of the
        talk-over who began speaking since holds it again at once, a new hold standing. Otherwise
        the hold is over."""
        self._talk.forget_words()
        if self._hold is not None and self._talk.talk_over_speaks:
            self._hold = _Hold()
            self._begin_stopping(self._hold)
            return True
        self._hold = None
        return False

    def _is_stale(self, told_changes: int | None) -> bool:
        return told_changes is not None and told_changes < self._talk.worded_changes

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
            or self._behind
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
    def choice_waits(self) -> bool:
        """A choice that plays waits for the talk-over."""
        return self._choice is not None

    @property
    def choice_may_play(self) -> bool:
        """A choice waits and no person of the talk-over speaks: ``play_choice`` gives its steps."""
        return self._choice is not None and not self._talk.talk_over_speaks

    @property
    def worded_changes(self) -> int:
        """The count of the talk-over's worded changes; it only rises."""
        return self._talk.worded_changes

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
