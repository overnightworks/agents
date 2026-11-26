"""The records of speaking and of being held, written once.

An app and an agent's runner say these to each other about a member: the arguments of speaking,
stopping and continuing in a place, what speaking and stopping answer (a sentence voiced, or
written as a line when the voice failed) and what each refuses, and what the window says when
people talk over the member (``TalkedOver``), when the member is held by it (``Interrupted``)
and whether the member's voice at the place has failed (``VoiceState``). ``agents.member``
re-exports every name. This module is base, so a runner parses all of it without the
``mcp-server`` extra.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, Field, StrictBool, StringConstraints

from agents.figure_vocabulary import FigureMood
from agents.member_place import PlaceRef
from agents.turn import NonEmptyUnicodeText, StrictModel, UnicodeText

__all__ = [
    "ContinueSpeakingArguments",
    "ContinueSpeakingRefusal",
    "ContinueSpeakingRefused",
    "ContinuedSpeaking",
    "DroppedSpeaking",
    "Interrupted",
    "MarkerTrue",
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
    "Written",
]


def _is_none(value: object) -> bool:
    return value is None


VoiceStyle = NonEmptyUnicodeText
"""How a sentence should sound: free text, such as ``louder and angrier``. The text is passed
through unchanged; the app neither bounds nor checks it, and its speech service owns the bound
and the check."""

SpokenLanguage = Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]
"""The language of a spoken sentence: a lowercase ISO 639-1 code of two letters, such as ``de`` or
``en``. The record checks the form only; whether the app's voice speaks the language is the app's
decision."""


class SpeakArguments(StrictModel):
    """The arguments of ``speak``: the place and one sentence, whose length the app bounds, and
    optionally the ``language`` of that sentence, the ``style`` it should sound in, which the app
    passes on unchanged, and the ``mood`` the agent's figure shows while it speaks; an absent one
    means the app's own default, and an app that draws no figure ignores the ``mood``.
    ``interrupted`` names the hold (``PlaceWindow.interrupted``) the sentence answers instead of
    the held rest; without it, a held member refuses the sentence."""

    place: PlaceRef
    text: NonEmptyUnicodeText
    language: SpokenLanguage | None = Field(default=None, exclude_if=_is_none)
    style: VoiceStyle | None = Field(default=None, exclude_if=_is_none)
    mood: Annotated[FigureMood, Field(strict=False)] | None = Field(
        default=None, exclude_if=_is_none
    )
    interrupted: NonEmptyUnicodeText | None = Field(default=None, exclude_if=_is_none)


def _refuse_false(value: bool) -> bool:
    if not value:
        raise ValueError("must be true")
    return value


MarkerTrue = Annotated[StrictBool, AfterValidator(_refuse_false)]
"""A marker field of an answer: exactly the JSON ``true``, so two answers stay distinct as JSON."""


class Spoken(StrictModel):
    """What ``speak`` answers, as JSON and without ``is_error``, when the sentence starts to play;
    the field is exactly the JSON ``true``. It carries no length: the sentence's end reaches the
    member as its own line in the window."""

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
    sentence names a hold that does not stand, and ``unavailable`` when the app neither voices nor
    writes the sentence now: a social reason, a limit of its own or a failed voice at a place
    without a chat. ``held`` and ``not_interrupted`` change nothing and answer at once. No
    refusal is ever turned into a written line."""

    NOT_ACTIVE = "not_active"
    TOO_LONG = "too_long"
    LANGUAGE_UNAVAILABLE = "language_unavailable"
    STYLE_UNAVAILABLE = "style_unavailable"
    STOPPED = "stopped"
    HELD = "held"
    NOT_INTERRUPTED = "not_interrupted"
    UNAVAILABLE = "unavailable"


class SpeakRefused(StrictModel):
    """What ``speak`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[SpeakRefusal, Field(strict=False)]


class StopSpeakingArguments(StrictModel):
    """The arguments of ``stop_speaking``: the place whose sentence and queue of the member are
    cut, and optionally ``interrupted``, the hold (``PlaceWindow.interrupted``) the stop ends; a
    stale one is refused."""

    place: PlaceRef
    interrupted: NonEmptyUnicodeText | None = Field(default=None, exclude_if=_is_none)


class StoppedSpeaking(StrictModel):
    """What ``stop_speaking`` answers on success, as JSON: the text of the cut sentence's line,
    the heard part when the app knows it, else the whole sentence."""

    cut: NonEmptyUnicodeText


class DroppedSpeaking(StrictModel):
    """What ``stop_speaking`` answers, as JSON, when it cut no sentence that had started: it
    dropped a held sentence that had not started, or the queue; the field is exactly the JSON
    ``true``."""

    dropped: MarkerTrue


StopSpeakingAnswer = StoppedSpeaking | DroppedSpeaking
"""What a ``stop_speaking`` that is not refused answers: a sentence was cut, or only the held or
waiting ones were dropped."""


class StopSpeakingRefusal(StrEnum):
    """Why the app does not stop: ``not_active`` when the member left, was removed or is paused;
    ``nothing_playing`` when the member is silent, so nothing was cut or dropped;
    ``not_interrupted`` when ``interrupted`` names a hold that does not stand."""

    NOT_ACTIVE = "not_active"
    NOTHING_PLAYING = "nothing_playing"
    NOT_INTERRUPTED = "not_interrupted"


class StopSpeakingRefused(StrictModel):
    """What ``stop_speaking`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[StopSpeakingRefusal, Field(strict=False)]


class ContinueSpeakingArguments(StrictModel):
    """The arguments of ``continue_speaking``: the place and the hold (``PlaceWindow.interrupted``)
    whose held sentence goes on."""

    place: PlaceRef
    interrupted: NonEmptyUnicodeText


class ContinuedSpeaking(StrictModel):
    """What ``continue_speaking`` answers on success, as JSON: the held sentence's rest plays,
    faded back in, then the queue; the field is exactly the JSON ``true``."""

    continued: MarkerTrue


class ContinueSpeakingRefusal(StrEnum):
    """Why the app does not continue: ``not_active`` when the member left, was removed or is
    paused; ``not_interrupted`` when ``interrupted`` names a hold that does not stand."""

    NOT_ACTIVE = "not_active"
    NOT_INTERRUPTED = "not_interrupted"


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


class Interrupted(StrictModel):
    """The member is held: a person talked over it, and nothing of the member plays until its
    decision. ``id`` is the app's, never repeats within the window's ``generation`` and stays
    fixed while the hold stands; ``continue_speaking``, ``stop_speaking`` and ``speak`` name it.
    The hold ends at one of them, or at the member's or the place's end."""

    id: NonEmptyUnicodeText


class VoiceState(StrEnum):
    """Whether the member's voice at the place has failed for that member there: ``available``
    while the app's voice works, ``unavailable`` while it has failed, so a ``speak`` would not be
    voiced for that reason. A social reason or a limit of the app never changes it."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
