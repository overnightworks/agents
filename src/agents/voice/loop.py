"""The voice loop: lines in, the host's wake rule, one decision at a time, sentences out.

A talk-over holds the answer; one more decision ends the hold, unless the talk-over held no word,
when the hold ends by itself."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Final

from logline import unit_trace

from agents.agent_file import AgentDefinition, TurnRequest
from agents.backend import AgentBackend, TurnPerson, TurnStopped
from agents.member import HoldPlace, PartialSpeech, PlaceEntry, SpokenLanguage, hold_place
from agents.voice import (
    DecisionEnded,
    DecisionFailed,
    EchoDropped,
    HearingEvent,
    Held,
    HoldEnded,
    HoldEnding,
    PartialsChanged,
    ReplyUnreadable,
    SentenceRefused,
    SentenceStarted,
    StayedSilent,
    VoiceEvent,
    VoiceOut,
    VoiceRefused,
    WakeRule,
    Woke,
)
from agents.voice.conversation import Conversation
from agents.voice.echo import is_echo
from agents.voice.reply import PROTOCOL_TEXT, SILENT_REPLY, HoldMarker, Marker, Reading, ReplyReader
from agents.voice.talk_over import TalkOver

_RECORDED_ENDINGS: Final[dict[HoldEnding, str | None]] = {
    HoldEnding.CONTINUED: f"[{Marker.CONTINUE}]",
    HoldEnding.FINISHED: f"[{Marker.FINISH}]",
    HoldEnding.STOPPED: SILENT_REPLY,
    HoldEnding.ANSWERED: None,
    HoldEnding.DROPPED: None,
}
"""What a hold decision is recorded as when it started no sentence; a drop leaves its lines."""

_PLAYING_ENDINGS: Final = frozenset(
    {HoldEnding.CONTINUED, HoldEnding.FINISHED, HoldEnding.ANSWERED}
)

_NOTHING_PLAYS: Final = hold_place(None, None)


async def run_voice_loop[Audio](
    agent: AgentDefinition,
    backend: AgentBackend,
    person: TurnPerson,
    hearing: AsyncIterator[HearingEvent],
    voice: VoiceOut[Audio],
    *,
    wakes: WakeRule,
    context: str,
    report: Callable[[VoiceEvent], None],
) -> None:
    """Hear a place's lines and partial speech and answer by voice until hearing ends.

    Every line enters one running conversation, but the agent's own spoken line and an echo: a
    spoken line that repeats the agent's latest answer (``voice.echo.is_echo``) is reported as
    ``EchoDropped`` and never asked of ``wakes``. An act line enters the conversation and is
    never asked of ``wakes`` either; it does not count for ``first_after_answer``. Any other
    line that is not the agent's own and that ``wakes`` accepts starts a decision; at most one
    is in flight and one answer plays, and a line that wakes meanwhile gets exactly one decision
    after it, over every line since the last one. A decision runs in the trace of the newest line
    that woke it (``PlaceEntry.traceparent``), a new trace when that line carries none. The system
    text is the agent's prompt, the protocol text and ``context``. A decision sends at most the
    agent's voice window of the conversation; only the newest line goes beyond it, alone and behind
    the window note once anything went. An agent without a voice section raises ``ValueError``
    before ``hearing`` is read.

    Each decision's turn is read to its end in its own task while its sentences play. They play
    in written order, and the next one renders while one plays, never more than one ahead. A
    ``VoiceRefused`` ends the answer: the turn is closed and the queued sentences dropped, a
    rendered one included; a render refused while the sentence before it plays is reported once
    that one has played. A ``TurnStopped`` is reported as ``DecisionFailed``, and the sentences
    already queued play out. A decision that played nothing and was not silent leaves its lines
    undecided until a line wakes the agent again.

    While an answer is written or plays, the first partial of a person who started speaking
    during it holds the answer (``Held``) and pauses the sound while hearing goes on; once the
    sound has stopped, that talk-over has ended and the held turn has been read, one decision, told
    where the sound stopped, ends the hold with ``[continue]``, ``[finish]``, ``[silent]`` or new
    speech (``HoldEnded``); without it the rest is dropped, and a talk-over with no word ends its
    hold by itself once nobody speaks (``WENT_ON``). Each decision a waking line started ends with
    one ``DecisionEnded``, its holds included. A sentence renders in the answered line's language.

    When hearing ends, the loop closes the turn, stops the sound and returns. Any other error
    of a port or the backend, and a ``seq`` not above the last, closes the turn, stops the
    sound and is raised inside an ``ExceptionGroup``. On the host's cancel nothing more is
    reported, and none of these ends a decision with ``DecisionEnded``. A port that never returns,
    ``pause`` included, holds the answer while hearing goes on.
    """
    if agent.voice_window_bytes is None:
        raise ValueError(f"agent {agent.name}: the voice loop needs a voice section")
    conversation = Conversation(agent.voice_window_bytes)
    async with asyncio.TaskGroup() as group:
        loop = _VoiceLoop(
            agent, backend, person, conversation, voice, wakes, context, report, group
        )
        answering = group.create_task(loop.answer_when_woken())
        async for event in hearing:
            loop.hear(event)
        answering.cancel()


@dataclass
class _Answer:
    """What one decision played and how its reply read.

    A hold splits it into segments: the first is the waking decision's, each later one a hold
    decision's, which starts at ``mark`` sentences started and is recorded on its own.
    """

    played: list[str] = field(default_factory=list[str])
    playing: str | None = None
    openings: list[StayedSilent | ReplyUnreadable] = field(
        default_factory=list[StayedSilent | ReplyUnreadable]
    )
    ended_short: bool = False
    sound_refused: bool = False
    mark: int = 0
    held: bool = False
    marker: str | None = None

    def started(self) -> list[str]:
        """Every sentence that started playing and was not refused, the one playing included."""
        return self.played if self.playing is None else [*self.played, self.playing]

    def segment(self) -> list[str]:
        """The sentences that started since the last hold decision opened."""
        return self.started()[self.mark :]

    def begin_hold_segment(self) -> None:
        self.mark = len(self.started())
        self.held = True
        self.marker = None

    def drop_cancelled_paused(self) -> None:
        """A cancelled paused sentence no longer counts as started, so ``mark`` moves down."""
        self.mark = min(self.mark, len(self.started()))

    def recorded_at_pause(self) -> str | None:
        """What the segment is recorded as when a hold closes it, or ``None`` for undecided."""
        if self.segment():
            return " ".join(self.segment())
        return self.marker if self.held else None

    def recorded(self) -> str | None:
        """What the conversation records, or ``None`` when the lines stay undecided."""
        if self.held:
            return self.recorded_at_pause()
        if self.played:
            return " ".join(self.played)
        silent = self.openings and all(isinstance(read, StayedSilent) for read in self.openings)
        return SILENT_REPLY if silent and not self.ended_short else None


@dataclass
class _Stage:
    """One turn being read: its sentences, those written and not started, and its ending."""

    holding: bool
    language: SpokenLanguage | None
    sentences: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue[str | None])
    unsaid: list[str] = field(default_factory=list[str])
    ending: asyncio.Future[HoldEnding] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    cut: bool = False
    reading: asyncio.Task[None] | None = None

    def decide(self, ending: HoldEnding) -> None:
        """A hold decision's first opening or sentence ends the hold; later ones change nothing."""
        if self.holding and not self.ending.done():
            self.ending.set_result(ending)

    @property
    def speaks(self) -> bool:
        return not self.holding or self.ending.result() is HoldEnding.ANSWERED


def _ending_of(opening: StayedSilent | ReplyUnreadable | HoldMarker) -> HoldEnding:
    if opening is Marker.CONTINUE:
        return HoldEnding.CONTINUED
    if opening is Marker.FINISH:
        return HoldEnding.FINISHED
    return HoldEnding.STOPPED if isinstance(opening, StayedSilent) else HoldEnding.DROPPED


class _VoiceLoop[Audio]:
    def __init__(
        self,
        agent: AgentDefinition,
        backend: AgentBackend,
        person: TurnPerson,
        conversation: Conversation,
        voice: VoiceOut[Audio],
        wakes: WakeRule,
        context: str,
        report: Callable[[VoiceEvent], None],
        group: asyncio.TaskGroup,
    ) -> None:
        self._agent = agent
        self._backend = backend
        self._person = person
        self._voice = voice
        self._wakes = wakes
        self._report = report
        self._group = group
        self._appended_system = "\n\n".join(part for part in (PROTOCOL_TEXT, context) if part)
        self._conversation = conversation
        self._woken = asyncio.Event()
        self._waking_traceparent: str | None = None
        self._first_after_answer = False
        self._answer = _Answer()
        self._last_played: list[str] = []
        self._talk = TalkOver()
        self._held = False
        self._stopping: asyncio.Task[HoldPlace] | None = None
        self._place = _NOTHING_PLAYS
        self._go = asyncio.Event()
        self._go.set()
        self._moved = asyncio.Event()
        self._stages: list[_Stage] = []

    def hear(self, event: HearingEvent) -> None:
        if isinstance(event, PartialsChanged):
            self._hear_partials(event.speech)
        else:
            self._hear_line(event)
        self._signal()

    def _signal(self) -> None:
        moved, self._moved = self._moved, asyncio.Event()
        moved.set()

    async def _until(self, holds: Callable[[], bool]) -> None:
        while not holds():
            await self._moved.wait()

    def _hear_partials(self, speech: tuple[PartialSpeech, ...]) -> None:
        self._talk.partials(speech)
        if self._held or not self._talk.standing:
            return
        self._held = True
        self._go.clear()
        self._held_now()

    def _held_now(self) -> None:
        """Report the hold and start stopping the sentence playing, if one is."""
        self._report(Held())
        sentence = self._answer.playing
        if sentence is None:
            self._place = _NOTHING_PLAYS
            return
        # Eager, so `pause` is entered while this sentence's `play` has not returned.
        self._stopping = self._group.create_task(self._stop_sound(sentence), eager_start=True)

    async def _stop_sound(self, sentence: str) -> HoldPlace:
        """Stop the sound of ``sentence``; the place of the hold, from what every listener heard."""
        return hold_place(sentence, await self._voice.pause())

    async def _where_the_sound_stopped(self) -> HoldPlace:
        """The latest place the sound stopped at, once the pause in flight has returned."""
        if self._stopping is not None:
            self._place = await self._stopping
            self._stopping = None
        return self._place

    async def _stop_stopping(self) -> None:
        if self._stopping is not None:
            self._stopping.cancel()
            await asyncio.wait({self._stopping})
            self._stopping = None

    def _hear_line(self, line: PlaceEntry) -> None:
        if not line.own:
            self._talk.finished(line.name, line.text)
        if is_echo(line, self._latest_answer()):
            self._conversation.skip(line)
            self._report(EchoDropped(line.text))
            return
        self._conversation.hear(line)
        if line.own or line.act:
            return
        first_after_answer, self._first_after_answer = self._first_after_answer, False
        if self._wakes(line, first_after_answer=first_after_answer):
            self._waking_traceparent = line.traceparent
            self._woken.set()

    def _latest_answer(self) -> list[str]:
        """What the most recent decision that played at least one sentence has started.

        During a hold it is what the hold decision has started, and until it starts one, what the
        held answer started.
        """
        return self._answer.segment() or self._answer.started() or self._last_played

    async def answer_when_woken(self) -> None:
        while True:
            await self._woken.wait()
            self._woken.clear()
            self._first_after_answer = False
            with unit_trace(self._waking_traceparent):
                played = await self._decide()
            self._first_after_answer = played

    async def _decide(self) -> bool:
        """Run one decision to its end, its holds included; whether it played a sentence."""
        self._report(Woke())
        self._talk.answer_begins()
        answer = self._answer = _Answer()
        self._stages = []
        try:
            await self._speak(self._start_stage())
            self._finish_answer()
            if not answer.sound_refused:  # a refused sound closes the turns, as a cancel does
                await asyncio.wait(self._readings())
        finally:
            self._finish_answer()
            await self._stop_stopping()
            readings = self._readings()
            for reading in readings:
                reading.cancel()
            await asyncio.wait(readings)
        self._conversation.close_decision(answer.recorded())
        if answer.played:
            self._last_played = answer.played
        self._report(DecisionEnded())
        return bool(answer.played)

    def _readings(self) -> list[asyncio.Task[None]]:
        """Every turn of the answer so far, a hold decision's still being written included."""
        return [stage.reading for stage in self._stages if stage.reading is not None]

    def _finish_answer(self) -> None:
        self._held = False
        self._go.set()
        self._talk.answer_ends()

    def _start_stage(self, *, holding: bool = False) -> _Stage:
        messages = self._conversation.open_decision()
        request = self._agent.request(messages, appended_system=self._appended_system)
        stage = _Stage(holding, self._conversation.answered_language())
        stage.reading = self._group.create_task(self._read(request, stage))
        self._stages.append(stage)
        return stage

    def _start_play(self, stage: _Stage) -> asyncio.Task[None]:
        play = self._group.create_task(self._play(stage))
        play.add_done_callback(lambda _: self._signal())
        return play

    async def _speak(self, stage: _Stage) -> None:
        """Play the stage's sentences; a talk-over holds them until a decision ends the hold."""
        play: asyncio.Task[None] | None = self._start_play(stage)
        try:
            while play is not None:
                await self._until(partial(self._held_or_done, play))
                if not self._held:
                    return await play
                held = await self._hold(stage, play)
                stage, play = held if held is not None else (stage, None)
        finally:
            if play is not None:
                await self._stop(play)

    def _held_or_done(self, play: asyncio.Task[None]) -> bool:
        return self._held or play.done()

    async def _stop(self, play: asyncio.Task[None] | None) -> None:
        if play is not None:
            play.cancel()
            await asyncio.wait({play})

    async def _hold(
        self, stage: _Stage, play: asyncio.Task[None]
    ) -> tuple[_Stage, asyncio.Task[None]] | None:
        """Hold the answer until a decision, or the talk-over's end without a word, ends the hold;
        what goes on, or ``None`` if nothing."""
        held_play: asyncio.Task[None] | None = play
        while True:
            place = await self._where_the_sound_stopped()
            await self._until(lambda: self._talk.worded or self._talk.nobody_speaks)
            if not self._talk.worded:
                return await self._go_on(stage, held_play)
            await asyncio.wait(self._readings())
            await self._until(lambda: not self._talk.standing)
            self._talk.forget_words()
            decision = self._open_hold(stage, place)
            ending = await decision.ending
            self._report(HoldEnded(ending))
            self._answer.marker = _RECORDED_ENDINGS[ending]
            if ending not in _PLAYING_ENDINGS or not self._talk.standing:
                return await self._end_hold(ending, stage, decision, held_play)
            self._report(Held())
            if ending is HoldEnding.ANSWERED:
                await self._stop(held_play)
                self._answer.drop_cancelled_paused()
                self._place = _NOTHING_PLAYS
                stage, held_play = decision, None
            elif ending is HoldEnding.FINISHED:
                stage.cut = True
                stage.unsaid.clear()

    def _open_hold(self, stage: _Stage, place: HoldPlace) -> _Stage:
        """Open the one decision on a talk-over over the held answer and the lines heard."""
        self._report(Woke())
        answer = self._answer
        self._conversation.close_decision(answer.recorded_at_pause())
        answer.begin_hold_segment()
        self._conversation.note_hold(place, stage.unsaid)
        self._woken.clear()
        return self._start_stage(holding=True)

    async def _end_hold(
        self,
        ending: HoldEnding,
        stage: _Stage,
        decision: _Stage,
        play: asyncio.Task[None] | None,
    ) -> tuple[_Stage, asyncio.Task[None]] | None:
        """Carry out an ending; the stage and play that go on, or ``None`` if the answer is over."""
        if ending in (HoldEnding.CONTINUED, HoldEnding.FINISHED):
            stage.cut = stage.cut or ending is HoldEnding.FINISHED
            if not await self._resumed(play):
                return None
        else:
            await self._stop(play)
            self._answer.drop_cancelled_paused()
        if ending in (HoldEnding.STOPPED, HoldEnding.DROPPED):
            self._finish_answer()
            return None
        if ending is HoldEnding.ANSWERED:
            stage, play = decision, None
        return self._carry_on(stage, play)

    async def _go_on(
        self, stage: _Stage, play: asyncio.Task[None] | None
    ) -> tuple[_Stage, asyncio.Task[None]] | None:
        """End a hold whose talk-over held no word by itself, as ``[continue]`` would; no decision
        is open, so the next one is told the agent went on."""
        self._report(HoldEnded(HoldEnding.WENT_ON))
        self._conversation.note_went_on()
        if not await self._resumed(play):
            return None
        return self._carry_on(stage, play)

    def _carry_on(
        self, stage: _Stage, play: asyncio.Task[None] | None
    ) -> tuple[_Stage, asyncio.Task[None]]:
        """Play the stage's sentences on, starting its play when none runs, and hold the answer
        again if a talk-over stands, else release it."""
        play = play or self._start_play(stage)
        if self._talk.standing:
            self._held_now()
        else:
            self._held = False
            self._go.set()
            self._talk.forget_words()
        return stage, play

    async def _resumed(self, play: asyncio.Task[None] | None) -> bool:
        """Resume the paused sound; ``False`` once the host refused and the answer is over."""
        if play is None or not self._answer.playing:
            return True
        sentence = self._answer.playing
        try:
            await self._voice.resume()
        except VoiceRefused:
            await self._stop(play)
            self._answer.ended_short = True
            self._answer.sound_refused = True
            self._report(SentenceRefused(sentence))
            self._finish_answer()
            return False
        return True

    async def _read(self, request: TurnRequest, stage: _Stage) -> None:
        reader = ReplyReader(holding=stage.holding)
        try:
            async with contextlib.aclosing(self._backend.run(request, self._person)) as events:
                async for event in events:
                    self._take(reader.read(event), stage)
        except TurnStopped as stopped:
            self._answer.ended_short = True
            self._report(
                DecisionFailed(
                    stopped.kind,
                    stopped.code,
                    stopped.detail,
                    retry_after_s=stopped.retry_after_s,
                )
            )
        stage.sentences.put_nowait(None)
        stage.decide(HoldEnding.DROPPED)

    def _take(self, reading: Reading, stage: _Stage) -> None:
        opening = reading.opening
        if opening is not None:
            if isinstance(opening, StayedSilent | ReplyUnreadable):
                if not stage.holding:
                    self._answer.openings.append(opening)
                self._report(opening)
            stage.decide(_ending_of(opening))
        if not reading.sentences:
            return
        stage.decide(HoldEnding.ANSWERED)
        if stage.speaks:
            for sentence in reading.sentences:
                stage.unsaid.append(sentence)
                stage.sentences.put_nowait(sentence)

    async def _play(self, stage: _Stage) -> None:
        """Play the sentences in written order; the next one renders while one plays."""
        ahead = self._group.create_task(self._render_next(stage))
        try:
            while not stage.cut and (rendered := await ahead) is not None:
                # A set event wakes its waiter even when it is cleared again before the waiter runs.
                while not self._go.is_set():
                    await self._go.wait()
                if stage.cut:
                    return
                if isinstance(rendered, SentenceRefused):
                    refused = rendered
                else:
                    ahead = self._group.create_task(self._render_next(stage))
                    refused = await self._play_one(*rendered, stage)
                if refused is not None:
                    self._answer.ended_short = True
                    self._answer.sound_refused = True
                    self._report(refused)
                    return
        finally:
            ahead.cancel()
            await asyncio.wait({ahead})

    async def _render_next(self, stage: _Stage) -> tuple[str, Audio] | SentenceRefused | None:
        """The next sentence and its sound, the refusal of its render, or ``None`` at the end."""
        if (sentence := await stage.sentences.get()) is None:
            return None
        try:
            return sentence, await self._voice.render(sentence, language=stage.language)
        except VoiceRefused:
            return SentenceRefused(sentence)

    async def _play_one(self, sentence: str, audio: Audio, stage: _Stage) -> SentenceRefused | None:
        """Play one rendered sentence; the refusal of its play, or ``None`` once it ended."""
        answer = self._answer
        answer.playing = sentence
        stage.unsaid.pop(0)
        self._report(SentenceStarted(sentence))
        try:
            await self._voice.play(audio)
        except VoiceRefused:
            return SentenceRefused(sentence)
        finally:
            answer.playing = None
        answer.played.append(sentence)
        return None
