"""Who talks over the agent: the loop's pure record of the utterances heard at a place.

A person's utterance runs from the first partial that shows them to their finished line or the
first snapshot without them. One that began while the agent answered is part of a talk-over; one
that began before the answer never is, however long it goes on. The host gives a person's partial
only once their voice has lasted its bound, so a shorter voice never reaches this module. Whether
a talk-over held a word is judged by person: from their first partial until ``forget_words``, a
partial or a finished line of a person of the talk-over that holds a word (``has_words``) counts,
also after their partial went. Sound never reaches this module.
"""

from __future__ import annotations

from collections.abc import Sequence

from agents.member import PartialSpeech, has_words


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
