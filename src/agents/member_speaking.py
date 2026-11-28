"""The records of speaking and of being held, written once.

An app and an agent's runner say these to each other about a member: the arguments of speaking,
stopping and continuing in a place, what speaking and stopping answer (a sentence voiced, or
written as a line when the voice failed) and what each refuses, and what the window says when
people talk over the member (``TalkedOver``), when the member is held by it (``Interrupted``)
and whether the member's voice at the place has failed (``VoiceState``); a hold says where it
stopped the member in a ``HoldPlace``, and a hold the app ended by itself is a ``WentOn``.
``words_of`` is the one notion of a word, and ``has_words`` its test. ``agents.member`` re-exports
the records. This module is base, so a runner parses all of it without the ``mcp-server`` extra.
"""

from __future__ import annotations

from enum import StrEnum
from itertools import groupby
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from agents.figure_vocabulary import FigureMood
from agents.member_place import PlaceRef
from agents.turn import MarkerTrue, NonEmptyUnicodeText, StrictModel, UnicodeText, is_false, is_none

__all__ = [
    "ContinueSpeakingArguments",
    "ContinueSpeakingRefusal",
    "ContinueSpeakingRefused",
    "ContinuedSpeaking",
    "DroppedSpeaking",
    "HoldPlace",
    "HoldPoint",
    "Interrupted",
    "PartialSpeech",
    "SpeakAnswer",
    "SpeakArguments",
    "SpeakRefusal",
    "SpeakRefused",
    "Spoken",
    "SpokenLanguage",
    "StopSpeakingAnswer",
    "StopSpeakingArguments",
    "StopSpeakingRefusal",
    "StopSpeakingRefused",
    "StoppedSpeaking",
    "TalkedOver",
    "VoiceState",
    "VoiceStyle",
    "WentOn",
    "Written",
    "has_words",
    "hold_place",
    "words_of",
]


def words_of(text: str) -> tuple[str, ...]:
    """The words of the text in lower case: its runs of characters that are a letter or a digit
    (``str.isalpha`` or ``str.isdigit``).

    A digit is what ``str.isdigit`` accepts, so a superscript such as "²" counts too, which errs
    toward a decision. Punctuation, an emoji, a fraction such as "½" and a Roman numeral sign such
    as "Ⅷ" (number characters, neither letter nor digit) hold no word.
    """
    runs = groupby(text, lambda character: character.isalpha() or character.isdigit())
    return tuple("".join(run).casefold() for is_word, run in runs if is_word)


def has_words(text: str) -> bool:
    """Whether the text holds a word (``words_of``). It is the one test of whether a talk-over
    held a word, for the wire and the voice loop alike."""
    return bool(words_of(text))


VoiceStyle = NonEmptyUnicodeText
"""How a sentence should sound: free text, such as ``louder and angrier``. The text is passed
through unchanged; the app neither bounds nor checks it, and its speech service owns the bound
and the check."""

SpokenLanguage = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]
"""The language of a spoken sentence: a lowercase ISO 639-1 code of two letters, such as ``de`` or
``en``. The record checks the form only; whether the app's voice speaks the language is the app's
decision."""


ChangeCount = Annotated[int, Field(ge=1)]
"""A count of worded changes: how many ``Interrupted.worded_changes`` a decision was told."""


def require_the_hold_the_sentence_answers(
    *, interrupted: str | None, keeps_queue: bool, told_changes: int | None
) -> None:
    """Raises ``ValueError`` when ``keeps_queue`` or ``told_changes`` is set without the hold the
    sentence answers."""
    if interrupted is None:
        if keeps_queue:
            raise ValueError("keeps_queue needs the hold the sentence answers")
        if told_changes is not None:
            raise ValueError("told_changes needs the hold the sentence answers")


class SpeakArguments(StrictModel):
    """The arguments of ``speak``: the place and one sentence, whose length the app bounds, and
    optionally the ``language`` of that sentence, the ``style`` it should sound in, which the app
    passes on unchanged, and the ``mood`` the agent's figure shows while it speaks; an absent one
    means the app's own default, and an app that draws no figure ignores the ``mood``.
    ``interrupted`` names the hold (``PlaceWindow.interrupted``) the sentence answers instead of
    the held rest; without it, a held member refuses the sentence. ``keeps_queue``, valid only with
    ``interrupted``, makes the sentence a short own line: it takes the head of the queue and the
    queued sentences stay behind it, none of them answering ``stopped``; absent while false, the
    sentence replaces the queue. An app that does not declare it leaves the queue to ``stopped``.
    ``told_changes``, valid only with ``interrupted``, is the ``Interrupted.worded_changes`` the
    runner's decision was told; absent, the app does not check it."""

    place: PlaceRef
    text: NonEmptyUnicodeText
    language: SpokenLanguage | None = Field(default=None, exclude_if=is_none)
    style: VoiceStyle | None = Field(default=None, exclude_if=is_none)
    mood: Annotated[FigureMood, Field(strict=False)] | None = Field(
        default=None, exclude_if=is_none
    )
    interrupted: NonEmptyUnicodeText | None = Field(default=None, exclude_if=is_none)
    keeps_queue: bool = Field(default=False, exclude_if=is_false)
    told_changes: ChangeCount | None = Field(default=None, exclude_if=is_none)

    @model_validator(mode="after")
    def _keep_the_queue_and_the_count_only_of_a_named_hold(self) -> Self:
        require_the_hold_the_sentence_answers(
            interrupted=self.interrupted,
            keeps_queue=self.keeps_queue,
            told_changes=self.told_changes,
        )
        return self


class Spoken(StrictModel):
    """What ``speak`` answers, as JSON and without ``is_error``, when the sentence starts to play;
    the field is exactly the JSON ``true``. It carries no length: the sentence's end reaches the
    member as its own line in the window, or, when not every seat heard a word of it, through the
    hold's place or the stop's answer."""

    spoken: MarkerTrue


class Written(StrictModel):
    """What ``speak`` answers, as JSON and without ``is_error``, when the app could not voice the
    sentence for a failure of its voice and posted it at the place as the member's written line
    instead; the field is exactly the JSON ``true``."""

    written: MarkerTrue


SpeakAnswer = Spoken | Written
"""What a ``speak`` that is not refused answers: the sentence started to play, or it was written."""


class SpeakRefusal(StrEnum):
    """Why the app does not speak a sentence: ``language_unavailable`` when the app's voice does
    not speak the language the sentence names, ``style_unavailable`` for every refusal of the
    ``style`` by the voice (it takes none, it is too long or it is not supported), ``stopped``
    for a queued sentence that a ``stop_speaking`` or a ``speak`` naming the hold dropped,
    ``held`` when the member is held and the sentence names no hold, ``not_interrupted`` when the
    sentence names a hold that does not stand or its waiting choice was voided or replaced,
    ``stale`` when the sentence's ``told_changes`` is below the ``worded_changes`` the app
    counted, and ``unavailable`` when the app neither voices nor writes the sentence now: a
    social reason, a limit of its own or a failed voice at a place without a chat. ``held`` and
    ``stale`` change nothing and answer at once, as does ``not_interrupted`` for a hold that does
    not stand. No refusal is ever turned into a written line."""

    NOT_ACTIVE = "not_active"
    TOO_LONG = "too_long"
    LANGUAGE_UNAVAILABLE = "language_unavailable"
    STYLE_UNAVAILABLE = "style_unavailable"
    STOPPED = "stopped"
    HELD = "held"
    NOT_INTERRUPTED = "not_interrupted"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class SpeakRefused(StrictModel):
    """What ``speak`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[SpeakRefusal, Field(strict=False)]


class StopSpeakingArguments(StrictModel):
    """The arguments of ``stop_speaking``: the place whose sentence and queue of the member are
    cut, and optionally ``interrupted``, the hold (``PlaceWindow.interrupted``) the stop ends; one
    that does not stand is refused. ``told_changes`` is the ``Interrupted.worded_changes`` the
    runner's decision was told; absent, the app does not check it."""

    place: PlaceRef
    interrupted: NonEmptyUnicodeText | None = Field(default=None, exclude_if=is_none)
    told_changes: ChangeCount | None = Field(default=None, exclude_if=is_none)


class StoppedSpeaking(StrictModel):
    """What ``stop_speaking`` answers when a cut line entered, as JSON: the text of the cut
    sentence's line, the heard part when the app knows it, else the whole sentence."""

    cut: NonEmptyUnicodeText


class DroppedSpeaking(StrictModel):
    """What ``stop_speaking`` answers, as JSON, when no cut line entered: it dropped a held
    sentence that had not started or that not every seat heard a word of, or a playing one cut
    before that, or the queue; the field is exactly the JSON ``true``."""

    dropped: MarkerTrue


StopSpeakingAnswer = StoppedSpeaking | DroppedSpeaking
"""What a ``stop_speaking`` that is not refused answers: a sentence was cut, or only the held or
waiting ones were dropped."""


class StopSpeakingRefusal(StrEnum):
    """Why the app does not stop: ``not_active`` when the member left, was removed or is paused;
    ``nothing_playing`` when the member is silent, so nothing was cut or dropped;
    ``not_interrupted`` when ``interrupted`` names a hold that does not stand; ``stale`` when
    ``told_changes`` is below the ``worded_changes`` the app counted."""

    NOT_ACTIVE = "not_active"
    NOTHING_PLAYING = "nothing_playing"
    NOT_INTERRUPTED = "not_interrupted"
    STALE = "stale"


class StopSpeakingRefused(StrictModel):
    """What ``stop_speaking`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[StopSpeakingRefusal, Field(strict=False)]


class ContinueSpeakingArguments(StrictModel):
    """The arguments of ``continue_speaking``: the place and the hold (``PlaceWindow.interrupted``)
    whose held sentence goes on. With ``finish``, only the held sentence's rest plays and the queue
    is dropped, each dropped ``speak`` answering ``stopped``; absent while false, the queue plays
    too. ``told_changes`` is the ``Interrupted.worded_changes`` the runner's decision was told;
    absent, the app does not check it."""

    place: PlaceRef
    interrupted: NonEmptyUnicodeText
    finish: bool = Field(default=False, exclude_if=is_false)
    told_changes: ChangeCount | None = Field(default=None, exclude_if=is_none)


class ContinuedSpeaking(StrictModel):
    """What ``continue_speaking`` answers on success, as JSON, when the held sentence's rest plays
    on, faded back in, then the queue unless the call finished: the answer comes when it plays,
    which waits until no person of the hold's talk-over speaks; the field is exactly the JSON
    ``true``."""

    continued: MarkerTrue


class ContinueSpeakingRefusal(StrEnum):
    """Why the app does not continue: ``not_active`` when the member left, was removed or is
    paused; ``not_interrupted`` when ``interrupted`` names a hold that does not stand, or a
    waiting call that a later choice, a stop or a voice that went on voided; ``stale`` when
    ``told_changes`` is below the ``worded_changes`` the app counted."""

    NOT_ACTIVE = "not_active"
    NOT_INTERRUPTED = "not_interrupted"
    STALE = "stale"


class ContinueSpeakingRefused(StrictModel):
    """What ``continue_speaking`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[ContinueSpeakingRefusal, Field(strict=False)]


class PartialSpeech(StrictModel):
    """What one person has said so far in an utterance that is not finished; provisional, never a
    line. ``text`` is empty while the person has started and said no words yet."""

    name: NonEmptyUnicodeText
    text: UnicodeText


class TalkedOver(StrictModel):
    """People talk over the member. ``id`` is the app's, never repeats within the window's
    ``generation`` and stays fixed from the record's appearance to its removal, so a runner takes
    one decision per ``id``; a second person who starts speaking adds a ``PartialSpeech`` under
    the same ``id``. ``playing`` says some sentence of the member plays at the place now. ``speech``
    lists the people in the order they started speaking."""

    id: NonEmptyUnicodeText
    playing: bool
    speech: tuple[PartialSpeech, ...] = Field(min_length=1)


class HoldPoint(StrEnum):
    """Where a hold stopped the member: ``in_sentence``, before the end of a sentence that answered
    ``Spoken``, or ``between_sentences``, when no such sentence was playing or the playing one
    played to its end. A sentence still rendering counts as waiting."""

    IN_SENTENCE = "in_sentence"
    BETWEEN_SENTENCES = "between_sentences"


class HoldPlace(StrictModel):
    """Where a hold stopped the member.

    ``in_sentence`` requires the ``sentence`` the hold stopped before its end, and says with
    ``heard_to`` how far every seat heard it: ``0 <= heard_to < len(sentence)`` in code points,
    where 0 means not every seat heard a word; absent, the app cannot tell. ``between_sentences``
    carries neither field. That ``heard_to`` falls on a word's end is the app's duty."""

    at: Annotated[HoldPoint, Field(strict=False)]
    sentence: NonEmptyUnicodeText | None = Field(default=None, exclude_if=is_none)
    heard_to: Annotated[int, Field(ge=0)] | None = Field(default=None, exclude_if=is_none)

    @model_validator(mode="after")
    def _keep_the_invariants_of_the_point(self) -> Self:
        if self.at is HoldPoint.BETWEEN_SENTENCES:
            if self.sentence is not None or self.heard_to is not None:
                raise ValueError("between_sentences carries no sentence and no heard_to")
        elif self.sentence is None:
            raise ValueError("in_sentence needs the sentence the hold stopped")
        elif self.heard_to is not None and self.heard_to >= len(self.sentence):
            raise ValueError("heard_to must stay below the sentence's length in code points")
        return self


def hold_place(sentence: str | None, heard_to: int | None) -> HoldPlace:
    """The place of a hold that found ``sentence`` playing, ``None`` when none was, and of which
    every seat heard ``heard_to`` code points, ``None`` when the app cannot tell.

    No sentence, or one heard to its end, gives ``between_sentences``; any other gives
    ``in_sentence``. A ``heard_to`` outside the sentence, or any ``heard_to`` without one, raises
    ``ValueError``.
    """
    if sentence is None:
        return HoldPlace(at=HoldPoint.BETWEEN_SENTENCES, heard_to=heard_to)
    if heard_to == len(sentence):
        return HoldPlace(at=HoldPoint.BETWEEN_SENTENCES)
    return HoldPlace(at=HoldPoint.IN_SENTENCE, sentence=sentence, heard_to=heard_to)


class Interrupted(StrictModel):
    """The member is held: a person talked over it, and nothing of the member plays until its
    decision. ``id`` is the app's, never repeats within the window's ``generation`` and stays
    fixed while the hold stands; ``continue_speaking``, ``stop_speaking`` and ``speak`` name it.
    The hold ends at one of them, or at the member's or the place's end. ``place`` says where
    the hold stopped the member and stays as it was while the hold stands; it is absent while
    the app states none. ``wordless`` is true from the change that adds the hold while no word
    (``has_words``) of its talk-over has been heard; the app then owns the hold's end and ends it
    by itself (``PlaceWindow.went_on``), and a runner starts no decision for it. It leaves in
    the change in which the first word is heard, and never returns for this ``id``; it is
    absent while false. ``worded_changes`` counts the partials and lines of the talk-over's people
    whose words (``words_of``) differ from what that person was last heard to say; it states the
    count from the change in which the first word is heard, only rises, and is absent while the
    app states none. A choice names the count its decision was told as ``told_changes``."""

    id: NonEmptyUnicodeText
    place: HoldPlace | None = Field(default=None, exclude_if=is_none)
    wordless: bool = Field(default=False, exclude_if=is_false)
    worded_changes: ChangeCount | None = Field(default=None, exclude_if=is_none)


class WentOn(StrictModel):
    """A hold through whose talk-over no word was heard ended by itself and the member went on:
    ``id`` is that hold's ``Interrupted.id``."""

    id: NonEmptyUnicodeText


class VoiceState(StrEnum):
    """Whether the member's voice at the place has failed for that member there: ``available``
    while the app's voice works, ``unavailable`` while it has failed, so a ``speak`` would not be
    voiced for that reason. A social reason or a limit of the app never changes it."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
