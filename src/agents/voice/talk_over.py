"""Who talks over the agent: the loop's pure record of the utterances heard at a place.

A person's utterance runs from the first partial that shows them to their finished line or the
first snapshot without them. One that began while the agent answered is part of a talk-over; one
that began before the answer never is, however long it goes on. Sound never reaches this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from agents.member import PartialSpeech


@dataclass(frozen=True)
class _Utterance:
    began_during_answer: bool
    has_words: bool


class TalkOver:
    """The utterances at a place and which of them talk over an answer."""

    def __init__(self) -> None:
        self._answering = False
        self._utterances: dict[str, _Utterance] = {}

    def answer_begins(self) -> None:
        """An answer starts: every utterance so far began before it."""
        self._answering = True
        self._utterances = {
            name: replace(utterance, began_during_answer=False)
            for name, utterance in self._utterances.items()
        }

    def answer_ends(self) -> None:
        self._answering = False

    def partials(self, speech: Sequence[PartialSpeech]) -> None:
        """The partial speech at the place now: whoever is not in it has ended their utterance."""
        known = self._utterances
        self._utterances = {
            partial.name: _Utterance(
                began_during_answer=known[partial.name].began_during_answer
                if partial.name in known
                else self._answering,
                has_words=_has_words(partial.text)
                or (partial.name in known and known[partial.name].has_words),
            )
            for partial in speech
        }

    def finished(self, name: str) -> None:
        """A finished line of ``name`` ends their utterance."""
        self._utterances.pop(name, None)

    @property
    def standing(self) -> bool:
        """Some utterance that began during the answer has not ended."""
        return any(utterance.began_during_answer for utterance in self._utterances.values())

    @property
    def worded(self) -> bool:
        """Some utterance that began during the answer has shown a word."""
        return any(
            utterance.began_during_answer and utterance.has_words
            for utterance in self._utterances.values()
        )


def _has_words(text: str) -> bool:
    return any(character.isalnum() for character in text)
