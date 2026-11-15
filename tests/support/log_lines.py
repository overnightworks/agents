"""Reading the JSON lines a step writes under logline's testing helper, through one reader."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from contextlib import ExitStack

import pytest
from logline.testing import configured

type LogLine = dict[str, object]


class LogLines:
    """Every line logline wrote to stdout since the fixture began, parsed."""

    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    def everything(self) -> str:
        """The text as written, for the absence checks."""
        return self._stream.getvalue()

    def all(self) -> list[LogLine]:
        return [json.loads(line) for line in self.everything().splitlines()]

    def of(self, event: str) -> list[LogLine]:
        return [line for line in self.all() if line["event"] == event]

    def one(self, event: str) -> LogLine:
        (line,) = self.of(event)
        return line


@pytest.fixture
def log_lines(monkeypatch: pytest.MonkeyPatch) -> Iterator[LogLines]:
    """The lines of the test, at every level: ``configure`` runs as a host's entry point does.

    ``configure`` binds the stdout it finds, so the fixture swaps in its own stream for that call
    alone and the test keeps pytest's capture.
    """
    stream = io.StringIO()
    monkeypatch.setenv("LOG_LEVEL", "debug")
    with ExitStack() as stack:
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdout", stream)
            stack.enter_context(configured("agents-tests"))
        yield LogLines(stream)
