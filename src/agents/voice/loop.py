"""The voice loop: lines in, the host's wake rule, one decision at a time, sentences out.

A talk-over holds the answer and one more decision ends the hold."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final

from logline import unit_trace

from agents.agent_file import AgentDefinition, TurnRequest
from agents.backend import AgentBackend, TurnPerson, TurnStopped
from agents.member import PartialSpeech, PlaceEntry
from agents.turn import TurnMessage
from agents.voice import (
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

    While an answer is written or plays, the first word of a person who started speaking during
    it pauses the answer (``Held``); once that talk-over has ended and the held turn has been
    read, one decision ends the hold with ``[continue]``, ``[finish]``, ``[silent]`` or new
    speech (``HoldEnded``); without a decision the rest is dropped. A host that yields no
    ``PartialsChanged`` is never held.

    When hearing ends, the loop closes the turn, stops the sound and returns. Any other error
    of a port or the backend, and a ``seq`` not above the last, closes the turn, stops the
    sound and is raised inside an ``ExceptionGroup``. On the host's cancel nothing more is
    reported. A port that never returns holds the answer while hearing goes on.
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
            await loop.hear(event)
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
        self._go = asyncio.Event()
        self._go.set()
        self._moved = asyncio.Event()
        self._stages: list[_Stage] = []

    async def hear(self, event: HearingEvent) -> None:
        if isinstance(event, PartialsChanged):
            await self._hear_partials(event.speech)
        else:
            self._hear_line(event)
        self._signal()

    def _signal(self) -> None:
        moved, self._moved = self._moved, asyncio.Event()
        moved.set()

    async def _until(self, holds: Callable[[], bool]) -> None:
        while not holds():
            await self._moved.wait()

    async def _hear_partials(self, speech: tuple[PartialSpeech, ...]) -> None:
        self._talk.partials(speech)
        if self._held or not self._talk.worded:
            return
        self._held = True
        self._go.clear()
        await self._held_now()

    async def _held_now(self) -> None:
        """Report the hold and pause the sentence playing, if one is."""
        self._report(Held())
        if self._answer.playing is not None:
            await self._voice.pause()

    def _hear_line(self, line: PlaceEntry) -> None:
        if not line.own:
            self._talk.finished(line.name)
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
            await self._speak(self._start_stage(self._conversation.open_decision()))
            self._finish_answer()
            if not answer.ended_short:  # a refused sound closes the turns, as a cancel does
                await asyncio.wait(self._readings())
        finally:
            self._finish_answer()
            readings = self._readings()
            for reading in readings:
                reading.cancel()
            await asyncio.wait(readings)
        self._conversation.close_decision(answer.recorded())
        if answer.played:
            self._last_played = answer.played
        return bool(answer.played)

    def _readings(self) -> list[asyncio.Task[None]]:
        """Every turn of the answer so far, a hold decision's still being written included."""
        return [stage.reading for stage in self._stages if stage.reading is not None]

    def _finish_answer(self) -> None:
        self._held = False
        self._go.set()
        self._talk.answer_ends()

    def _start_stage(self, messages: list[TurnMessage], *, holding: bool = False) -> _Stage:
        request = self._agent.request(messages, appended_system=self._appended_system)
        stage = _Stage(holding)
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
                await self._until(lambda: self._held or play is None or play.done())
                if not self._held:
                    return await play
                held = await self._hold(stage, play)
                stage, play = held if held is not None else (stage, None)
        finally:
            if play is not None:
                await self._stop(play)

    async def _stop(self, play: asyncio.Task[None] | None) -> None:
        if play is not None:
            play.cancel()
            await asyncio.wait({play})

    async def _hold(
        self, stage: _Stage, play: asyncio.Task[None]
    ) -> tuple[_Stage, asyncio.Task[None]] | None:
        """Hold the answer until a decision ends the hold; what goes on, or ``None`` if nothing."""
        held_play: asyncio.Task[None] | None = play
        while True:
            await asyncio.wait(self._readings())
            await self._until(lambda: not self._talk.standing)
            decision = self._open_hold(stage)
            ending = await decision.ending
            self._report(HoldEnded(ending))
            self._answer.marker = _RECORDED_ENDINGS[ending]
            if ending not in _PLAYING_ENDINGS or not self._talk.worded:
                return await self._end_hold(ending, stage, decision, held_play)
            self._report(Held())
            if ending is HoldEnding.ANSWERED:
                await self._stop(held_play)
                self._answer.drop_cancelled_paused()
                stage, held_play = decision, None
            elif ending is HoldEnding.FINISHED:
                stage.cut = True
                stage.unsaid.clear()

    def _open_hold(self, stage: _Stage) -> _Stage:
        """Open the one decision on a talk-over over the held answer and the lines heard."""
        self._report(Woke())
        answer = self._answer
        self._conversation.close_decision(answer.recorded_at_pause())
        paused_at = answer.playing
        answer.begin_hold_segment()
        self._conversation.note_pause(paused_at, stage.unsaid)
        self._woken.clear()
        return self._start_stage(self._conversation.open_decision(), holding=True)

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
        play = play or self._start_play(stage)
        if self._talk.worded:
            await self._held_now()
        else:
            self._held = False
            self._go.set()
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
            self._report(DecisionFailed(stopped.kind, stopped.code, stopped.detail))
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
            return sentence, await self._voice.render(sentence)
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
