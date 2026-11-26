"""What the voice loop's tests share: a trace, a recording voice, a paced gateway and a run.

Every run drives ``run_voice_loop`` with the real ``GatewayBackend`` over a scripted transport.
The trace holds, in order, what the loop reported, what it called on the voice, what it asked
the wake rule and what the paced gateway saw, so a test waits on and asserts outcomes only. The
lines and answers live in ``voice_lines``, which every test imports directly.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx2
import pytest

from agents.agent_file import AgentDefinition
from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, Reply, completion, tool_call, tool_round
from agents.backend.gateway import GatewayBackend, GatewayDetail
from agents.member import PlaceEntry
from agents.tools import HandTool, ToolDeclaration, ToolEffect, ToolOutcome
from agents.turn import FailureCode
from agents.voice import DecisionFailed, HearingEvent, Held, HoldEnded, VoiceEvent, WakeRule, Woke
from agents.voice.loop import run_voice_loop
from tests.support.gateway_turn_support import a_chunk, a_gateway_config, a_person, an_event_stream
from tests.support.virtual_time import VirtualTimeLoop
from tests.support.voice_lines import a_line, speaking

PROMPT: Final = "Du hörst zu und antwortest kurz."
CONTEXT: Final = "Im Raum sind Ada und Ben."
_SETTLE_SECONDS: Final = 1.0
_REAL_TIME_BOUND_SECONDS: Final = 10.0
_VOICE_EVENTS: Final = VoiceEvent.__value__

A_GATEWAY_REFUSAL: Final = Reply(status=400, body=b'{"error": {"type": "budget_exceeded"}}')
"""A decision the gateway refuses with a 400: the turn ends failed, not as a spent budget."""
A_REFUSED_TURN: Final = DecisionFailed(
    TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE, GatewayDetail.GATEWAY_REFUSED
)
"""What the loop reports for a decision answered with ``A_GATEWAY_REFUSAL``."""
A_SILENT_ROUND: Final = tool_round(tool_call("call-1", "look_up", {}), content="[silent]")
"""A tool round that calls ``look_up`` and says ``[silent]``."""
THE_FIRST_ANSWER: Final = completion("Eins. Zwei.")

type Wait = Callable[[], Awaitable[object]]
type StreamPart = bytes | Wait | Exception


def piece(text: str) -> bytes:
    """One content chunk of a streamed round."""
    return an_event_stream(a_chunk(text), done=False)


def finish(reason: str = "stop") -> bytes:
    """The finish chunk and ``[DONE]``."""
    return an_event_stream(a_chunk(finish_reason=reason))


async def hang() -> None:
    await asyncio.Event().wait()


def a_lookup_tool(trace: Trace, *, takes_a_moment: bool = False) -> HandTool:
    """A tool that records ``("tool", "look_up")`` when called; slow, also when it has answered."""

    async def look_up(principal, arguments):
        trace.record(("tool", "look_up"))
        if takes_a_moment:
            await trace.settled()
            trace.record(("tool", "answered"))
        return ToolOutcome(content="gefunden", is_error=False)

    declaration = ToolDeclaration(
        name="look_up",
        description="Looks something up.",
        parameters={"type": "object", "properties": {}},
        effect=ToolEffect.READ_ONLY,
    )
    return HandTool(declaration=declaration, handler=look_up)


class Trace:
    """Everything the loop did that a test may see, in order, and a way to wait for a state.

    Voice events are recorded as reported; the support's own entries are tuples that start
    with their kind: ``render``, ``unrendered`` (a render cancelled), ``play``, ``played``,
    ``stopped`` (a play cancelled), ``pause``, ``unpaused`` (a pause cancelled), ``resume``,
    ``asked``, ``tool``, ``written``, ``closed``, ``cancelled``, ``ended`` (the loop's run is
    over) and ``rendered_in`` (the language a sentence was rendered in).
    """

    def __init__(self) -> None:
        self.entries: list[object] = []
        self.loop_task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()

    def record(self, entry: object) -> None:
        self.entries.append(entry)
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def until(self, holds: Callable[[], bool]) -> None:
        while not holds():
            await self._changed.wait()

    async def settled(self) -> None:
        """Return once every other task waits; the virtual clock moves only then."""
        await asyncio.sleep(_SETTLE_SECONDS)

    async def written(self, text: str) -> None:
        self.record(("written", text))

    async def host_cancels(self) -> None:
        """The host cancels the loop; the hearing that awaits this is cancelled with it."""
        assert self.loop_task is not None
        self.record(("cancelled",))
        self.loop_task.cancel()
        await hang()

    @property
    def events(self) -> list[object]:
        return [entry for entry in self.entries if isinstance(entry, _VOICE_EVENTS)]

    @property
    def calls(self) -> list[tuple[str, str]]:
        kinds = {
            "render",
            "unrendered",
            "play",
            "played",
            "stopped",
            "pause",
            "unpaused",
            "resume",
        }
        return [entry for entry in self.entries if isinstance(entry, tuple) and entry[0] in kinds]

    @property
    def rendered_languages(self) -> list[tuple[str, str | None]]:
        return [entry[1:] for entry in self.entries if _kind_of(entry) == "rendered_in"]

    @property
    def asked(self) -> list[tuple[str, bool]]:
        return [entry[1:] for entry in self.entries if _kind_of(entry) == "asked"]

    @property
    def played(self) -> list[str]:
        return [entry[1] for entry in self.entries if _kind_of(entry) == "played"]

    @property
    def rendered_at_the_written_end(self) -> list[str]:
        """Each sentence rendered while nothing was written after it in its decision's reply."""
        at_the_end: list[str] = []
        written, position = "", 0
        for entry in self.entries:
            if entry == Woke():
                written, position = "", 0
            elif _kind_of(entry) == "written":
                written += entry[1]
            elif _kind_of(entry) == "render":
                position = written.index(entry[1], position) + len(entry[1])
                if not written[position:].strip():
                    at_the_end.append(entry[1])
        return at_the_end

    def count(self, entry: object) -> int:
        return self.entries.count(entry)

    def order_of(self, *marks: object) -> list[object]:
        """The entries that are one of ``marks``, in the order they were recorded."""
        return [entry for entry in self.entries if entry in marks]

    def events_after(self, mark: object) -> list[object]:
        """The voice events recorded after ``mark`` first was."""
        later = self.entries[self.entries.index(mark) + 1 :]
        return [entry for entry in later if isinstance(entry, _VOICE_EVENTS)]


def _kind_of(entry: object) -> str | None:
    return entry[0] if isinstance(entry, tuple) else None


class RecordingVoice:
    """A ``VoiceOut`` whose sound is the sentence itself; it records each call in the trace.

    Every render takes ``seconds_per_render``, every play ``seconds_per_sentence`` and every
    resume ``seconds_per_resume`` on the loop's clock, and a sentence in ``held`` plays until
    ``release`` ends it. A play does not return while paused, until ``resume``; a sentence in
    ``ends_at_pause`` ends as it is paused, so its ``resume`` has nothing to go on with. A pause
    returns what ``heard_to`` maps its sentence to (``None``, the voice cannot tell, when absent),
    and the length of a sentence in ``ends_at_pause``. After that time, ``raises`` maps a call and
    its sentence to what that call raises, and a call in ``hangs`` never returns.
    """

    def __init__(
        self,
        trace: Trace,
        *,
        raises: dict[tuple[str, str], Exception] | None = None,
        hangs: Collection[tuple[str, str]] = (),
        held: Collection[str] = (),
        ends_at_pause: Collection[str] = (),
        heard_to: Mapping[str, int | None] | None = None,
        seconds_per_render: float = 0.0,
        seconds_per_sentence: float = 0.0,
        seconds_per_pause: float = 0.0,
        seconds_per_resume: float = 0.0,
    ) -> None:
        self._trace = trace
        self._raises = raises or {}
        self._hangs = hangs
        self._held = {sentence: asyncio.Event() for sentence in held}
        self._ends_at_pause = ends_at_pause
        self._heard_to = heard_to or {}
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._sentence = ""
        self._render_seconds = seconds_per_render
        self._seconds = seconds_per_sentence
        self._pause_seconds = seconds_per_pause
        self._resume_seconds = seconds_per_resume

    def release(self, sentence: str) -> None:
        self._held[sentence].set()

    async def render(self, sentence: str, *, language: str | None) -> str:
        try:
            self._trace.record(("render", sentence))
            self._trace.record(("rendered_in", sentence, language))
            if self._render_seconds:
                await asyncio.sleep(self._render_seconds)
            await self._as_arranged("render", sentence)
        except asyncio.CancelledError:
            self._trace.record(("unrendered", sentence))
            raise
        return sentence

    async def pause(self) -> int | None:
        sentence = self._sentence
        try:
            self._trace.record(("pause", sentence))
            if self._pause_seconds:
                await asyncio.sleep(self._pause_seconds)
            await self._as_arranged("pause", sentence)
        except asyncio.CancelledError:
            self._trace.record(("unpaused", sentence))
            raise
        if sentence in self._ends_at_pause:
            if sentence in self._held:
                self._held[sentence].set()
            return len(sentence)
        self._resumed.clear()
        return self._heard_to.get(sentence)

    async def resume(self) -> None:
        self._trace.record(("resume", self._sentence))
        if self._resume_seconds:
            await asyncio.sleep(self._resume_seconds)
        await self._as_arranged("resume", self._sentence)
        self._resumed.set()

    async def play(self, audio: str) -> None:
        try:
            self._trace.record(("play", audio))
            self._sentence = audio
            self._resumed.set()
            if audio in self._held:
                await self._held[audio].wait()
            if self._seconds:
                await asyncio.sleep(self._seconds)
            await self._resumed.wait()
            await self._as_arranged("play", audio)
        except asyncio.CancelledError:
            self._trace.record(("stopped", audio))
            raise
        self._trace.record(("played", audio))

    async def _as_arranged(self, call: str, sentence: str) -> None:
        if (call, sentence) in self._raises:
            raise self._raises[call, sentence]
        if (call, sentence) in self._hangs:
            await hang()


class PacedGateway(httpx2.AsyncBaseTransport):
    """Answers each request with its next streamed reply, the last one repeating.

    A reply is a sequence of parts: event-stream bytes, sent as one chunk; a wait, awaited before
    the next part; or an exception, which breaks the stream. The trace records ``closed`` when a
    reply's body is closed, whether it ended or the turn was closed.
    """

    def __init__(self, trace: Trace, *replies: Sequence[StreamPart]) -> None:
        self._trace = trace
        self._replies = list(replies)
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        await request.aread()
        self.requests.append(request)
        parts = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        headers = {"content-type": "text/event-stream"}
        return httpx2.Response(200, headers=headers, stream=_PacedBody(parts, self._trace))


class _PacedBody(httpx2.AsyncByteStream):
    def __init__(self, parts: Sequence[StreamPart], trace: Trace) -> None:
        self._parts = parts
        self._trace = trace

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self._parts:
            if isinstance(part, bytes):
                yield part
            elif isinstance(part, Exception):
                raise part
            else:
                await part()

    async def aclose(self) -> None:
        self._trace.record(("closed",))


def written_piece_by_piece(trace: Trace, texts: Sequence[str]) -> list[StreamPart]:
    """A reply that traces each piece as written, sends it and lets the loop settle on it."""
    parts: list[StreamPart] = []
    for text in texts:
        parts += [functools.partial(trace.written, text), piece(text), trace.settled]
    return [*parts, finish()]


async def _stream_one_reply(
    parts: Sequence[StreamPart], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Answer one request with ``parts`` as one chunked event stream, then close."""
    head = await reader.readuntil(b"\r\n\r\n")
    length = re.search(rb"content-length: *(\d+)", head, re.IGNORECASE)
    await reader.readexactly(int(length[1]) if length else 0)
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
        b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    )
    for part in parts:
        if isinstance(part, bytes):
            writer.write(b"%x\r\n%s\r\n" % (len(part), part))
            await writer.drain()
        elif isinstance(part, Exception):
            raise part
        else:
            await part()
    writer.write(b"0\r\n\r\n")
    await writer.drain()
    writer.close()
    await writer.wait_closed()


def messages_of(request: httpx2.Request) -> list[dict]:
    """The messages a request sent the gateway: the system message first."""
    return json.loads(request.content)["messages"]


def rows_of(message: dict) -> list[dict]:
    """The lines of a user message, one JSON object each."""
    return [json.loads(row) for row in message["content"].splitlines()]


def texts_of(message: dict) -> list[str | dict]:
    """The lines' texts; a line of the loop's own, which has no text, as its keys and values."""
    return [row.get("text", row) for row in rows_of(message)]


def conversation_of(request: httpx2.Request) -> list[tuple[str, list[str | dict] | str]]:
    """A request's messages after the system one: a user message as its lines' texts."""
    return [
        (message["role"], texts_of(message) if message["role"] == "user" else message["content"])
        for message in messages_of(request)[1:]
    ]


@dataclass(frozen=True)
class Ending:
    """How the loop ended: returned, raised, or cancelled by the host."""

    raised: BaseException | None = None
    cancelled: bool = False


RETURNED: Final = Ending()

type Hearing = Callable[[Trace], AsyncIterator[HearingEvent]]


@dataclass
class VoicePlace:
    """The agent, its person and gateway configuration, and the trace of one loop run.

    ``window_bytes`` is the agent's voice window, ``None`` for an agent without a voice section;
    the default is wider than any conversation a test here holds.
    """

    directory: Path
    trace: Trace = field(default_factory=Trace)
    window_bytes: int | None = 1_000_000

    def agent(self, tools: Sequence[HandTool] = ()) -> AgentDefinition:
        return AgentDefinition(
            name="listener",
            description="Hears a conversation and answers by voice.",
            roles=["admin", "user"],
            tools=tuple(tool.declaration for tool in tools),
            prompt=PROMPT,
            model="text/standin",
            max_duration_seconds=60,
            max_tool_calls=3 if tools else None,
            voice_window_bytes=self.window_bytes,
        )

    def run(
        self,
        hearing: Hearing,
        transport: httpx2.AsyncBaseTransport,
        *,
        waking: Collection[str] | None = None,
        wakes: WakeRule | None = None,
        voice: RecordingVoice | None = None,
        tools: Sequence[HandTool] = (),
    ) -> Ending:
        """Run the loop to its end in virtual time; ``waking`` names the texts that wake.

        A ``wakes`` rule given decides instead, and giving both raises ``ValueError``; with
        neither, every line wakes. Either way the trace records every line the loop asks.
        """
        config = a_gateway_config(self.directory)
        backend = GatewayBackend(config, tools=tools, transport=transport)
        rule = self._wake_rule(waking, wakes)
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            return runner.run(self._ended(hearing, backend, rule, voice, tools))

    def run_over_a_socket(self, hearing: Hearing, parts: Sequence[StreamPart]) -> Ending:
        """Run the loop on the wall clock against a loopback gateway that streams ``parts``."""

        async def main() -> Ending:
            server = await asyncio.start_server(
                functools.partial(_stream_one_reply, parts), "127.0.0.1", 0
            )
            port = server.sockets[0].getsockname()[1]
            config = a_gateway_config(self.directory)
            gateway = config.gateway.model_copy(update={"origin": f"http://127.0.0.1:{port}"})
            async with (
                server,
                GatewayBackend(config.model_copy(update={"gateway": gateway})) as backend,
            ):
                try:
                    return await self._ended(
                        hearing, backend, self._wake_rule(None, None), None, ()
                    )
                finally:
                    server.close_clients()

        return asyncio.run(asyncio.wait_for(main(), _REAL_TIME_BOUND_SECONDS))

    async def _ended(
        self,
        hearing: Hearing,
        backend: GatewayBackend,
        wakes: WakeRule,
        voice: RecordingVoice | None,
        tools: Sequence[HandTool],
    ) -> Ending:
        loop = asyncio.create_task(
            run_voice_loop(
                self.agent(tools),
                backend,
                a_person(),
                hearing(self.trace),
                RecordingVoice(self.trace) if voice is None else voice,
                wakes=wakes,
                context=CONTEXT,
                report=self.trace.record,
            )
        )
        self.trace.loop_task = loop
        try:
            await asyncio.wait({loop})
        finally:
            loop.cancel()
        self.trace.record(("ended",))
        if loop.cancelled():
            return Ending(cancelled=True)
        return Ending(raised=loop.exception())

    def _wake_rule(self, waking: Collection[str] | None, rule: WakeRule | None) -> WakeRule:
        if waking is not None and rule is not None:
            raise ValueError("a run takes the texts that wake or a wake rule, not both")

        def wakes(line: PlaceEntry, *, first_after_answer: bool) -> bool:
            self.trace.record(("asked", line.text, first_after_answer))
            if rule is not None:
                return rule(line, first_after_answer=first_after_answer)
            return waking is None or line.text in waking

        return wakes


def an_answer_of_two_that_is_playing(place, *later_replies):
    """A voice that plays ``Eins.`` until released and a gateway whose first reply has both."""
    voice = RecordingVoice(place.trace, held={"Eins."})
    return voice, FakeGateway(THE_FIRST_ANSWER, *later_replies)


async def interjection_while_playing(trace):
    """The first line wakes the agent; a person then starts talking over its playing sentence."""
    yield a_line(1, "Erzähl.")
    await trace.until(lambda: ("play", "Eins.") in trace.entries)
    yield speaking(("Ben", "Moment"))
    await trace.until(lambda: Held() in trace.entries)


async def the_person_finishes(trace, *, text="Moment, wie meinst du das?", seq=2, language=None):
    yield a_line(seq, text, name="Ben", language=language)
    yield speaking()


async def the_hold_ends(trace, voice):
    """A talk-over that ended: the person's line and then no partial; the held sound is freed."""
    async for event in interjection_while_playing(trace):
        yield event
    async for event in the_person_finishes(trace):
        yield event
    await trace.until(lambda: any(isinstance(entry, HoldEnded) for entry in trace.entries))
    voice.release("Eins.")
    await trace.settled()


@pytest.fixture
def place(tmp_path) -> VoicePlace:
    return VoicePlace(tmp_path)
