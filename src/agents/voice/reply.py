"""How the loop reads a reply: the marker words, the rules that teach them, the sentences.

This module is the one owner of the marker words and of the reply's rules (``REPLY_RULES``);
the protocol text is composed from them.
A reply is read per round: a round opening with ``[`` holds until its marker
closes within 16 characters, and any other round is speech, released sentence by sentence.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import Final, Literal

from agents.backend import TurnEvent
from agents.turn_events import AssistantTextEvent, FinalEvent, ToolCallEvent
from agents.voice import ReplyUnreadable, StayedSilent, sentences
from agents.voice.conversation import WINDOW_NOTE


class Marker(StrEnum):
    """A word a round of the reply may open with, between square brackets."""

    SILENT = "silent"
    CONTINUE = "continue"
    FINISH = "finish"


type HoldMarker = Literal[Marker.CONTINUE, Marker.FINISH]
"""The markers only a decision on a talk-over reads: go on, or finish the sentence and stop."""

SILENT_REPLY: Final = f"[{Marker.SILENT}]"
"""A reply that says nothing; the conversation records a silent decision as this."""

REPLY_RULES: Final = (
    "Your reply is spoken aloud sentence by sentence and heard once, so write plain spoken "
    "sentences without markup, lists, headings or emoji.\n"
    "Write only the words to say aloud, never what you are doing or why.\n"
    f"To say nothing, reply with {SILENT_REPLY} and nothing else."
)
"""What a runner appends when a ``ReplyReader`` reads its reply: the rules of a spoken reply."""

HOLD_RULES: Final = (
    "When someone talks over you, your reply is paused and you are asked again: the last user "
    'message then ends with a line that has the keys "paused_at" (the sentence you were saying, '
    'or null) and "unsaid" (the sentences you had written and not yet said). Open that reply '
    f"with [{Marker.CONTINUE}] to go on from where you paused, [{Marker.FINISH}] to finish the "
    f"sentence you were saying and stop, {SILENT_REPLY} to stop, or with the words to say "
    "instead, which stop what you were saying."
)
"""What a runner tells the model about a paused reply; the holding reader reads these openings."""

PROTOCOL_TEXT: Final = (
    "You take part in a conversation by voice. Each user message holds what was heard or "
    'written since your last reply, one JSON object per line: "name" is who it comes from, '
    '"kind" is what kind of line it is in the app\'s words, "own" marks your own line, '
    '"spoken" marks a line said aloud rather than written, "language" is the ISO 639-1 code a '
    'spoken line was heard in, "act" marks a line that says what someone did rather than what '
    'someone said, "text" is the line itself, and "missed_before" counts the lines that were '
    "not passed to you before it.\n"
    "Your reply is spoken in the language of the newest line of the last user message that has "
    'a "name" and is neither "own" nor an "act"; write it in that language, and when that line '
    'has no "language", in the language the context names.\n'
    f"Once the conversation grows long, the first user message opens with {WINDOW_NOTE}; "
    "nobody said that line.\n"
    f"Your earlier replies hold only what was spoken aloud, or {SILENT_REPLY}; a paused reply "
    "holds what was spoken after the pause, or the marker it opened with.\n"
    f"After a tool call, reply {SILENT_REPLY} when you have nothing to add.\n"
    f"{HOLD_RULES}\n"
    f"{REPLY_RULES}"
)
"""Appended to the agent's prompt: the line format and note, the loop's replies, the reply rules."""

_MARKER_OPEN: Final = "["
_MARKER_CLOSE: Final = "]"
_MARKER_REACH: Final = 16
_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_TERMINATOR_WITH_CONTENT_AFTER: Final = re.compile(r"[.!?…:;\n]\s*\S")
# The splitter judges a word by the words before it; fewer than this many made it split
# differently than on the whole text.
_CONTEXT_WORDS: Final = 8


class SentenceRelease:
    """Holds a round's speech and releases a sentence only once content follows it.

    The sentence at the end stays held because the model may still be writing it: ``Dr.`` at
    the end is not yet a boundary. Only the last word can still turn into one, so each piece
    re-judges that word, a few words before it and what follows it. Text with whitespace costs
    linear time; a stretch without whitespace is one word, re-judged whole on every piece, so it
    costs quadratic time and only the turn's deadline bounds it.
    """

    def __init__(self, split: Callable[[str], tuple[str, ...]]) -> None:
        self._split = split
        self._buffer = ""
        self._undecided_from = 0

    def feed(self, piece: str) -> tuple[str, ...]:
        """Add a piece; return the sentences it completed."""
        text = (self._buffer + piece).lstrip()
        start = self._undecided_from
        if _TERMINATOR_WITH_CONTENT_AFTER.search(text, start) is None:
            return self._hold(text, start)
        *completed, _held = self._split(text[start:]) or ("",)
        if not completed:
            return self._hold(text, start)
        end = start
        for sentence in completed:
            end = text.index(sentence, end) + len(sentence)
        first_end = text.index(completed[0], start) + len(completed[0])
        self._hold(text[end:].lstrip(), 0)
        return (text[:first_end].strip(), *completed[1:])

    def flush(self) -> tuple[str, ...]:
        """Return what is left once the round has ended."""
        rest = self._buffer.strip()
        self._buffer = ""
        self._undecided_from = 0
        return (rest,) if rest else ()

    def _hold(self, text: str, searched_from: int) -> tuple[str, ...]:
        self._buffer = text
        self._undecided_from = _last_word_start(text, searched_from)
        return ()


@dataclass(frozen=True)
class Reading:
    """What one event of the turn released: whole sentences, and a round's marker once read."""

    sentences: tuple[str, ...] = ()
    opening: StayedSilent | ReplyUnreadable | HoldMarker | None = None


class _Round(Enum):
    OPENING = auto()
    SPEECH = auto()
    MUTED = auto()


class ReplyReader:
    """Reads one turn's events round by round; a tool call ends a round and the next opens.

    Leading whitespace is skipped. A round that opens with ``[`` holds its text until ``]``
    closes the marker within 16 characters; the word between, trimmed of spaces and folded to
    ASCII lower case, is the marker. ``silent`` mutes the round; any other word, no ``]`` in
    reach or a round that ends first mutes it as unreadable. Any other round is speech, and a
    ``[`` inside it is text, except that a sentence that is nothing but the ``silent`` marker is
    dropped; the sentences before it were released and are spoken. A reader made with
    ``holding`` also reads ``continue`` and ``finish`` as their markers, which mute the round
    too; any other reader reads them as unreadable.
    """

    def __init__(self, *, holding: bool = False) -> None:
        self._holding = holding
        self._start_round()

    def read(self, event: TurnEvent) -> Reading:
        if isinstance(event, AssistantTextEvent):
            return self._piece(event.text)
        if isinstance(event, ToolCallEvent | FinalEvent):
            return self._end_round()
        return Reading()

    def _start_round(self) -> None:
        self._round = _Round.OPENING
        self._opening = ""
        self._release = SentenceRelease(sentences.split_sentences)

    def _piece(self, text: str) -> Reading:
        if self._round is _Round.SPEECH:
            return Reading(_spoken(self._release.feed(text)))
        if self._round is _Round.MUTED:
            return Reading()
        self._opening = (self._opening + text).lstrip()
        if not self._opening:
            return Reading()
        if not self._opening.startswith(_MARKER_OPEN):
            self._round = _Round.SPEECH
            return Reading(_spoken(self._release.feed(self._opening)))
        opening = _marker_of(self._opening, holding=self._holding)
        if opening is not None:
            self._round = _Round.MUTED
        return Reading(opening=opening)

    def _end_round(self) -> Reading:
        if self._round is _Round.SPEECH:
            reading = Reading(_spoken(self._release.flush()))
        elif self._round is _Round.OPENING and self._opening:
            reading = Reading(opening=ReplyUnreadable())
        else:
            reading = Reading()
        self._start_round()
        return reading


def _marker_of(
    opening: str, *, holding: bool = False
) -> StayedSilent | ReplyUnreadable | HoldMarker | None:
    """How a round opening with ``[`` reads, or ``None`` while its marker may still close."""
    close = opening.find(_MARKER_CLOSE, 1, _MARKER_REACH + 1)
    if close == -1:
        return ReplyUnreadable() if len(opening) > _MARKER_REACH else None
    word = opening[1:close].strip(" ").translate(_ASCII_LOWER)
    if word == Marker.SILENT:
        return StayedSilent()
    if holding and word == Marker.CONTINUE:
        return Marker.CONTINUE
    if holding and word == Marker.FINISH:
        return Marker.FINISH
    return ReplyUnreadable()


def _spoken(released: tuple[str, ...]) -> tuple[str, ...]:
    """The sentences without any that is nothing but the silent marker."""
    return tuple(sentence for sentence in released if not _is_silent_marker(sentence))


def _is_silent_marker(sentence: str) -> bool:
    return (
        sentence.startswith(_MARKER_OPEN)
        and sentence.find(_MARKER_CLOSE) == len(sentence) - 1
        and isinstance(_marker_of(sentence), StayedSilent)
    )


def _last_word_start(text: str, not_before: int) -> int:
    index = len(text.rstrip())
    for _ in range(_CONTEXT_WORDS):
        index = _skip_back(text, index, not_before, is_space=False)
        index = _skip_back(text, index, not_before, is_space=True)
    return _skip_back(text, index, not_before, is_space=False)


def _skip_back(text: str, index: int, not_before: int, *, is_space: bool) -> int:
    while index > not_before and text[index - 1].isspace() == is_space:
        index -= 1
    return index
