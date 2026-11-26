"""A conversation by voice, as text: the ports a host passes in and the events the loop reports.

``agents.voice.loop.run_voice_loop`` hears a place's lines, lets the host's
``WakeRule`` decide which line starts a decision, and speaks the agent's answer sentence by
sentence through the host's ``VoiceOut``. Sound never reaches this package: the hearing port
yields lines as ``PlaceEntry`` text and the partial speech at the place as ``PartialsChanged``,
and ``VoiceOut`` takes sentences, pauses and resumes them. It needs the ``voice`` extra, and
no base module imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from agents.backend import TurnStopKind
from agents.member import PartialSpeech, PlaceEntry, SpokenLanguage
from agents.turn import FailureCode, RejectionCode, StopOutcome, outcome_of_stop

__all__ = [
    "DecisionEnded",
    "DecisionFailed",
    "EchoDropped",
    "HearingEvent",
    "Held",
    "HoldEnded",
    "HoldEnding",
    "PartialsChanged",
    "ReplyUnreadable",
    "SentenceRefused",
    "SentenceStarted",
    "StayedSilent",
    "VoiceEvent",
    "VoiceOut",
    "VoiceRefused",
    "WakeRule",
    "Woke",
]


class VoiceRefused(Exception):
    """The host cannot speak this sentence now; the answer ends."""


class VoiceOut[Audio](Protocol):
    """How the host speaks one sentence: render it, then play what it rendered.

    The loop plays the sentences in order: ``play`` for a sentence starts only after it returned
    for the one before. While one sentence plays, the loop renders the next, never more than one
    ahead, so ``render`` may run while ``play`` does. A talk-over pauses the sound that plays and
    the loop then resumes it or stops it by cancelling its ``play``. The loop calls ``pause``
    only while a ``play`` has not returned, runs it in a task of its own while hearing goes on,
    and cancels it when hearing ends.
    """

    async def render(self, sentence: str, *, language: SpokenLanguage | None) -> Audio:
        """The sentence's sound, neither played nor queued: it is heard only through ``play``.

        ``language`` is the answered line's language: that of the newest line the decision sends
        that a person said or wrote; ``None`` when that line names none or the decision answers no
        such line, and the host speaks in its own default.
        """
        ...

    async def play(self, audio: Audio) -> None:
        """Return when the sound has ended; cancelling or refusing the play stops it at once.

        While the sound is paused the play does not return.
        """

    async def pause(self) -> int | None:
        """Stop the sound playing now where its next word ends, faded as the host's voice fades,
        and return what every listener heard of its sentence.

        The sound plays to its end instead when less than the host's bound of it remains; the
        remainder comes from the sentence's word marks or, for a sentence without word marks,
        from its finished sound. Otherwise it stops at its next word end, or at once for a
        sentence without word marks. A remainder not yet known counts as more than the bound.
        Return once the sound has stopped, for any reason, a refused ``play`` included: where
        the sound plays beyond a hop (a browser), the hop's time is the host's.

        The result is the number of code points of the sentence that every listener heard, cut
        back to a word's end: 0 when none heard a word, the sentence's length when it played to
        its end (its ``play`` then returns, and a sound that already ended gives this too) and
        ``None`` when the host cannot tell, as with no word marks; it is never estimated from
        time. A result above the sentence's length is a defect the loop raises.
        """
        ...

    async def resume(self) -> None:
        """Go on with the paused sound from where it paused, faded back in; from the sentence's
        start when its ``pause`` returned 0.

        Its ``play`` returns when it ends. When the paused sound had already ended, return at
        once; its ``play`` returns. Raises ``VoiceRefused`` when the host cannot go on.
        """
        ...


class WakeRule(Protocol):
    """Whether a line that is neither the agent's own, nor an echo, nor an act starts a decision.

    An act line is never asked and does not count for ``first_after_answer``, which is true for
    the first asked line heard after a decision that played at least one sentence ended, while
    no decision is in flight; it is false for every other line, a line heard while an answer
    plays included.
    """

    def __call__(self, line: PlaceEntry, *, first_after_answer: bool) -> bool: ...


@dataclass(frozen=True)
class PartialsChanged:
    """The partial speech at the place after a change.

    One ``PartialSpeech`` per person other than the agent whose finished line the host gives, in
    the order they started, empty when nobody speaks. A person is in it only once their voice has
    lasted the bound the host sets (about 0.4 s), so a shorter voice reaches the loop only as its
    line. The host gives each change whether or not the agent answers; a person's finished line
    comes before the change that drops that utterance's partial. The loop decides what a
    talk-over is.
    """

    speech: tuple[PartialSpeech, ...]


type HearingEvent = PlaceEntry | PartialsChanged
"""What the hearing port yields: a finished line, or a change of the partial speech."""


@dataclass(frozen=True)
class Woke:
    """A decision starts."""


@dataclass(frozen=True)
class StayedSilent:
    """A round of the reply opened with ``[silent]``."""


@dataclass(frozen=True)
class ReplyUnreadable:
    """A round of the reply opened with an unknown or unclosed marker; none of it is spoken."""


@dataclass(frozen=True)
class SentenceStarted:
    """A sentence starts playing: the loop hands its sound to ``VoiceOut.play``."""

    text: str


@dataclass(frozen=True)
class SentenceRefused:
    """``VoiceRefused`` ended the answer at this sentence."""

    text: str


@dataclass(frozen=True)
class DecisionFailed:
    """The decision's turn ended short with ``TurnStopped``; its kind, code and detail.

    ``retry_after_s`` is the wait a busy refusal names, as the gateway gave it; ``None`` for every
    other stop.
    """

    kind: TurnStopKind
    code: RejectionCode | FailureCode
    detail: str | None
    retry_after_s: int | None = field(default=None, kw_only=True)

    @property
    def outcome(self) -> StopOutcome:
        return outcome_of_stop(self.code)


@dataclass(frozen=True)
class DecisionEnded:
    """The last event of a decision a waking line started, its holds included.

    It comes after every other event of that decision (``Woke``, ``StayedSilent``,
    ``ReplyUnreadable``, ``SentenceStarted``, ``SentenceRefused``, ``DecisionFailed``, ``Held``,
    ``HoldEnded``). By then its sound has ended, each sentence it started having played to its
    end, been refused or been stopped, and every turn it started, a hold decision's and their
    tools included, has ended: read to its end, stopped with ``TurnStopped``, or closed after a
    refused sound. The next ``Woke`` comes after it. The first ``Woke`` after the loop starts or
    after a ``DecisionEnded`` opens a span; a ``Woke`` inside an open span is a hold decision.
    ``EchoDropped`` is hearing's and may come on either side. When the loop itself ends, none is
    reported.
    """


@dataclass(frozen=True)
class Held:
    """A talk-over holds the answer: the sound playing is stopped by ``VoiceOut.pause`` and no
    other sentence starts."""


class HoldEnding(StrEnum):
    """How a hold ends: the answer goes on, finishes its sentence, stops, is replaced or dropped."""

    CONTINUED = "continued"
    FINISHED = "finished"
    STOPPED = "stopped"
    ANSWERED = "answered"
    DROPPED = "dropped"


@dataclass(frozen=True)
class HoldEnded:
    """The decision on a talk-over ended the hold; reported before its effects.

    ``DROPPED`` is a failed decision or one that opened with nothing readable.
    """

    ending: HoldEnding


@dataclass(frozen=True)
class EchoDropped:
    """A spoken line repeated the agent's latest answer: it never enters and wakes nothing."""

    text: str


type VoiceEvent = (
    Woke
    | StayedSilent
    | ReplyUnreadable
    | SentenceStarted
    | SentenceRefused
    | DecisionFailed
    | DecisionEnded
    | EchoDropped
    | Held
    | HoldEnded
)
"""What the loop reports; a host may show, log or measure it. An in-process record."""
