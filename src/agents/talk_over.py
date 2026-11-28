"""The span, the utterances at a place and which of them talk over the member: the state of
``agents.floor``'s talk-over, pure and synchronous. Only ``agents.floor`` imports it."""

from __future__ import annotations

from dataclasses import dataclass

from agents.member_speaking import PartialSpeech, words_of

__all__ = ["TalkOver", "ToldTalkOver"]


@dataclass(frozen=True)
class ToldTalkOver:
    """What a hold decision is told of its talk-over: the ``changes`` the floor counted and the
    ``speech`` its people still say, each as a partial of theirs that holds a word."""

    changes: int
    speech: tuple[PartialSpeech, ...]


class TalkOver:
    """The span, the utterances at a place and which of them talk over the member."""

    def __init__(self) -> None:
        self._span_open = False
        self._began_in_span: dict[str, bool] = {}
        self._speaking: set[str] = set()
        self._talkers: set[str] = set()
        self._standing: dict[str, str] = {}
        self._heard: dict[str, tuple[str, ...]] = {}
        self._told: dict[str, tuple[str, ...]] = {}
        self._worded_changes = 0

    @property
    def span_open(self) -> bool:
        return self._span_open

    def span_opens(self) -> None:
        """A span opens: every utterance so far began before it."""
        self._span_open = True
        self._forget_what_began_in_the_span()

    def span_closes(self) -> None:
        """A span closes: no utterance is a talk-over any more, whatever it says next."""
        self._span_open = False
        self._forget_what_began_in_the_span()

    def _forget_what_began_in_the_span(self) -> None:
        self._began_in_span = dict.fromkeys(self._began_in_span, False)
        self.forget_words()

    def forget_words(self) -> None:
        """Count words afresh: the people of the talk-over are those whose partial stands and whose
        utterance began in the span, and what they have said is what their partials say. A voice
        that has not lasted the bound joins with its first partial."""
        self._standing = {
            key: text for key, text in self._standing.items() if self._began_in_span.get(key)
        }
        self._talkers = set(self._standing)
        self._heard = {
            key: words for key, text in self._standing.items() if (words := words_of(text))
        }

    def utterance_began(self, key: str) -> None:
        self._began_in_span[key] = self._span_open
        self._speaking.add(key)

    def utterance_ended(self, key: str) -> None:
        self._began_in_span.pop(key, None)
        self._speaking.discard(key)
        self._standing.pop(key, None)
        self._told.pop(key, None)

    def in_utterance(self, key: str) -> bool:
        """The key's utterance began and neither its line nor its end came since."""
        return key in self._began_in_span

    def is_talker(self, key: str) -> bool:
        """The key's person is part of the talk-over."""
        return key in self._talkers

    def partial(self, key: str, text: str) -> bool:
        """``key``'s voice lasted the bound; whether its utterance began during the span."""
        if key not in self._began_in_span:
            self.utterance_began(key)
            self._began_in_span[key] = False
        began_in_span = self._began_in_span[key]
        if began_in_span:
            self._talkers.add(key)
            self._standing[key] = text
            self._hears(key, words_of(text))
        return began_in_span

    def finished(self, key: str, text: str) -> bool:
        """A finished line of ``key`` ends their utterance as a talk-over; its words count when
        ``key`` is a person of the talk-over. Whether the line repeats the words a decision was
        told of ``key``."""
        self._began_in_span.pop(key, None)
        self._standing.pop(key, None)
        words = words_of(text)
        told = self._told.pop(key, None)
        if key in self._talkers:
            self._hears(key, words)
        return bool(words) and words == told

    def _hears(self, key: str, words: tuple[str, ...]) -> None:
        if words and words != self._heard.get(key):
            self._heard[key] = words
            self._worded_changes += 1

    def told(self) -> ToldTalkOver:
        """What a decision is told: the count and the standing partials with words, which a line
        of theirs may repeat until their utterance ends, without a change."""
        speech = tuple(
            PartialSpeech(name=key, text=text)
            for key, text in self._standing.items()
            if words_of(text)
        )
        self._told |= {partial.name: words_of(partial.text) for partial in speech}
        return ToldTalkOver(self._worded_changes, speech)

    @property
    def speaking(self) -> frozenset[str]:
        return frozenset(self._speaking)

    @property
    def standing(self) -> bool:
        """Some utterance that began during the span has not ended."""
        return any(self._began_in_span.values())

    @property
    def talk_over_speaks(self) -> bool:
        """A person of the talk-over has not ended their turn."""
        return not self._talkers.isdisjoint(self._speaking)

    @property
    def worded(self) -> bool:
        """A word of a person of the talk-over was heard since ``forget_words``."""
        return any(self._heard.values())

    @property
    def worded_changes(self) -> int:
        """The partials and lines of the talk-over's people whose words differ from what that
        person was last heard to say; it only rises."""
        return self._worded_changes

    @property
    def nobody_speaks(self) -> bool:
        """No person is speaking, whenever their utterance began."""
        return not self._speaking
