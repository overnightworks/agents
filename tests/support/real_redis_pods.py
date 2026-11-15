"""The pods of the real-Redis suites: a host process on its own port against one Redis, started,
waited for and killed by a test. ``tests/support/real_redis_pod.py`` is the host each one runs."""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PREFIX = "notes"
POD_A_USER = "pod-a"
POD_A_PASSWORD = "pod-a-secret"
WAIT_SECONDS = 20


@dataclass
class Pod:
    process: subprocess.Popen[bytes]
    url: str
    log: Path

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()

    def stop(self) -> int:
        """SIGTERM and the exit status, which is the signal itself once uvicorn shut down in order;
        a pod that does not leave is killed and fails the test."""
        self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise


def as_user(redis_url: str, user: str, password: str) -> str:
    scheme, address = redis_url.split("://", 1)
    return f"{scheme}://{user}:{password}@{address}"


def started(tmp_path: Path, redis_url: str) -> Pod:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = tmp_path / f"pod-{port}.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.support.real_redis_pod",
                str(port),
                redis_url,
                PREFIX,
            ],
            cwd=REPOSITORY,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    pod = Pod(process, f"http://127.0.0.1:{port}", log)
    with ExitStack() as killed_unless_ready:
        killed_unless_ready.callback(pod.kill)
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            assert process.poll() is None, log.read_text()
            with suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
                killed_unless_ready.pop_all()
                return pod
            time.sleep(0.1)
        pytest.fail(f"the pod on port {port} was not ready: {log.read_text()}")


def wait_until(check: Callable[[], bool], *, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "the awaited state never came"
        time.sleep(0.1)
