"""Who talks over the agent: the loop's pure record of the utterances heard at a place.

A person's utterance runs from the first partial that shows them to their finished line or the
first snapshot without them. One that began while the agent answered is part of a talk-over; one
that began before the answer never is, however long it goes on. The host gives a person's partial
only once their voice has lasted its bound, so a shorter voice never reaches this module. Sound
never reaches it either.
"""

from __future__ import annotations

from collections.abc import Sequence

from agents.member import PartialSpeech


class TalkOver:
    """The utterances at a place and which of them talk over an answer."""

    def __init__(self) -> None:
        self._answering = False
        self._began_during_answer: dict[str, bool] = {}

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

    def partials(self, speech: Sequence[PartialSpeech]) -> None:
        """The partial speech at the place now: whoever is not in it has ended their utterance."""
        known = self._began_during_answer
        self._began_during_answer = {
            partial.name: known.get(partial.name, self._answering) for partial in speech
        }

    def finished(self, name: str) -> None:
        """A finished line of ``name`` ends their utterance."""
        self._began_during_answer.pop(name, None)

    @property
    def standing(self) -> bool:
        """Some utterance that began during the answer has not ended."""
        return any(self._began_during_answer.values())
