"""One running conversation of a voice loop: its decided pairs and the lines still undecided.

It lives in memory while the loop runs and is stored nowhere. A decided pair is the user message
of a decision's lines and what the agent played, or the silent reply; what the model wrote and
did not play is never recorded. A decision sends at most ``window_bytes`` of it, counted as the
UTF-8 bytes of the messages' texts, because a provider's tokens cannot be counted here; only the
newest line goes beyond it, alone and behind ``WINDOW_NOTE`` once anything went. What a decision
dropped never returns. A decision on a paused answer also holds the loop's own line that says
where the sound stopped and what was unsaid; it is recorded with the decision's lines when the
decision is, and dropped with them when they stay undecided. A hold that went on by itself adds
the loop's own went-on line (``told_went_on``) to the undecided lines, which the next decision,
a hold decision of the same answer or the next waking one, opens over. The answered line of a
decision is the newest of the lines it sends that a person said or wrote: not the agent's own, an
act or one of the loop's two lines about a hold.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from agents.member import HoldPlace, PartialSpeech, PlaceEntry, SpokenLanguage
from agents.turn import JsonObject, TurnMessage, TurnRole

WINDOW_NOTE: Final = "(Earlier parts of this conversation are no longer shown.)"
"""The line that opens the first user message once the window has dropped earlier parts."""

_LINE_BREAK: Final = "\n"


class Heard(StrEnum):
    """How much of the sentence a hold stopped the loop's line says was heard."""

    PART = "part"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _Line:
    """One line of a decision as the model reads it, answerable when a person said or wrote it."""

    written: str
    answerable: bool
    language: SpokenLanguage | None = None


class Conversation:
    def __init__(self, window_bytes: int) -> None:
        self._window_bytes = window_bytes
        self._decided: list[TurnMessage] = []
        self._undecided: list[_Line] = []
        self._deciding: list[_Line] = []
        self._last_seq: int | None = None
        self._missed = 0
        self._noted = False
        self._pause_line: _Line | None = None

    def hear(self, line: PlaceEntry) -> None:
        """Enter a line in ``seq`` order; the agent's own spoken line is skipped.

        The loop records what it played itself. The next line that enters after a gap in ``seq``
        carries ``missed_before``, the number of lines not heard. Raises ``ValueError`` when
        ``seq`` is not above the last one.
        """
        self.skip(line)
        if line.own and line.spoken:
            return
        self._undecided.append(_line_heard(line, self._missed))
        self._missed = 0

    def skip(self, line: PlaceEntry) -> None:
        """Take ``line``'s place in ``seq`` order without entering it.

        A skipped line is never counted as missed, and a gap before it passes on to the next
        line that enters. Raises ``ValueError`` when ``seq`` is not above the last one.
        """
        if self._last_seq is not None and line.seq <= self._last_seq:
            raise ValueError(f"seq {line.seq} is not above the last seq {self._last_seq}")
        if self._last_seq is not None:
            self._missed += line.seq - self._last_seq - 1
        self._last_seq = line.seq

    def note_hold(
        self,
        place: HoldPlace,
        unsaid: Sequence[str],
        talked_over: Sequence[PartialSpeech] = (),
    ) -> None:
        """Add the loop's line about a held answer to the lines the next decision opens over.

        ``place`` is where the sound stopped, ``unsaid`` are the sentences written and not
        started and ``talked_over`` what people are still saying; the line tells them as
        ``told_hold`` does.
        """
        held = {"held": told_hold(place, unsaid, talked_over)}
        self._pause_line = _Line(json.dumps(held, ensure_ascii=False), answerable=False)
        self._undecided.append(self._pause_line)

    def note_went_on(self) -> None:
        """Add the loop's line that a hold ended by itself to the lines the next decision opens
        over; it stays undecided with them when that decision plays nothing."""
        self._undecided.append(_Line(json.dumps(told_went_on()), answerable=False))

    def open_decision(self) -> list[TurnMessage]:
        """The messages of a decision over every undecided line, ending with them.

        Beyond the window, the oldest decided pairs go until a quarter of it is free, then the
        oldest of these lines while they alone exceed it; the newest line always stays. Once
        anything went, ``WINDOW_NOTE`` opens the first user message.
        """
        self._deciding, self._undecided = self._undecided, []
        if self._size() > self._window_bytes:
            self._drop_the_oldest()
        messages = [*self._decided, _user_message(self._texts_deciding())]
        if self._noted:
            messages[0] = _user_message([WINDOW_NOTE, messages[0].text])
        return messages

    def answered_language(self) -> SpokenLanguage | None:
        """The language of the answered line of the open decision, after the window's drops.

        ``None`` when that line names none or the decision sends no such line; an older line's
        language never stands in for it.
        """
        for line in reversed(self._deciding):
            if line.answerable:
                return line.language
        return None

    def close_decision(self, reply: str | None) -> None:
        """Record the open decision's lines with ``reply``.

        With ``None`` they stay undecided, ahead of the lines heard since, without the pause line.
        """
        if reply is None:
            kept = [line for line in self._deciding if line is not self._pause_line]
            self._undecided = [*kept, *self._undecided]
        else:
            self._decided += [
                _user_message(self._texts_deciding()),
                TurnMessage(role=TurnRole.ASSISTANT, text=reply),
            ]
        self._deciding = []
        self._pause_line = None

    def _drop_the_oldest(self) -> None:
        if not self._decided and len(self._deciding) <= 1:
            return
        self._noted = True
        size = self._size()
        while self._decided and size > self._window_bytes * 3 / 4:
            user, assistant = self._decided[:2]
            del self._decided[:2]
            size -= _bytes_of(user.text) + _bytes_of(assistant.text)
        while len(self._deciding) > 1 and size > self._window_bytes:
            size -= _bytes_of(self._deciding.pop(0).written + _LINE_BREAK)

    def _texts_deciding(self) -> list[str]:
        return [line.written for line in self._deciding]

    def _size(self) -> int:
        """The UTF-8 bytes of the open decision's messages' texts, the note's line included."""
        note = _bytes_of(WINDOW_NOTE + _LINE_BREAK) if self._noted else 0
        decided = sum(_bytes_of(message.text) for message in self._decided)
        return note + decided + _bytes_of(_LINE_BREAK.join(self._texts_deciding()))


def told_hold(
    place: HoldPlace, unsaid: Sequence[str], talked_over: Sequence[PartialSpeech] = ()
) -> JsonObject:
    """What a hold decision is told of where the sound stopped, and the sentences not started.

    The part heard and the rest (``"heard": "part"`` with ``"said"`` and ``"rest"``), nothing
    heard (``"none"``, the whole sentence as ``"rest"``) or, when the app cannot tell, the whole
    ``"sentence"`` (``"unknown"``); ``Heard`` holds the three words. Between sentences it tells
    none of these. ``"unsaid"`` lists the sentences written and not started, and ``"talked_over"``,
    when ``talked_over`` is not empty, what people are still saying, each as ``"name"`` and
    ``"text"``. ``HOLD_RULES`` explains the keys to the model; a runner whose app states no place
    passes ``hold_place(its playing sentence, None)``.
    """
    told: JsonObject = {}
    if place.sentence is not None:
        told |= _told_sentence(place.sentence, place.heard_to)
    told["unsaid"] = list(unsaid)
    if talked_over:
        told["talked_over"] = [{"name": speech.name, "text": speech.text} for speech in talked_over]
    return told


def told_went_on() -> JsonObject:
    """What the next round is told once a sound without words held the agent and it went on by
    itself from where it stopped; ``HOLD_RULES`` explains the key to the model. A runner sends the
    same object."""
    return {"went_on": True}


def _told_sentence(sentence: str, heard_to: int | None) -> JsonObject:
    if heard_to is None:
        return {"heard": Heard.UNKNOWN, "sentence": sentence}
    if heard_to == 0:
        return {"heard": Heard.NONE, "rest": sentence}
    return {"heard": Heard.PART, "said": sentence[:heard_to], "rest": sentence[heard_to:]}


def _bytes_of(text: str) -> int:
    return len(text.encode())


def _line_heard(line: PlaceEntry, missed: int) -> _Line:
    fields: dict[str, object] = {
        "name": line.name,
        "kind": line.kind,
        "own": line.own,
        "spoken": line.spoken,
        "text": line.text,
    }
    if line.language is not None:
        fields["language"] = line.language
    if line.act:
        fields["act"] = True
    if missed:
        fields["missed_before"] = missed
    return _Line(
        json.dumps(fields, ensure_ascii=False),
        answerable=not (line.own or line.act),
        language=line.language,
    )


def _user_message(lines: list[str]) -> TurnMessage:
    return TurnMessage(role=TurnRole.USER, text=_LINE_BREAK.join(lines))
