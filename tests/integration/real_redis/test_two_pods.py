"""Two pods of one host on one real Redis: a write on one wakes a listen on the other.

``scripts/run_redis_tests.sh`` runs this directory against a throwaway Redis and passes its URL in
``AGENTS_TEST_REDIS_URL``. Each pod is its own uvicorn process; pod A connects as its own Redis
user, so a test can take away what that user may do.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import pytest
from redis import Redis

from agents.mcp_wire import MCP_ENDPOINT_PATH
from agents.member import STANDING_URI
from tests.integration.real_redis.pod import WRITE_PATH, WRITES_KEY, WRITTEN_MEMBER
from tests.support.mcp_server_support import member_scope, rpc_body

_REPOSITORY = Path(__file__).resolve().parents[3]
_PREFIX = "notes"
_POD_A_USER = "pod-a"
_POD_A_PASSWORD = "pod-a-secret"
_ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
_UPDATED = "notifications/resources/updated"
_WAIT_SECONDS = 20


@dataclass
class Pod:
    process: subprocess.Popen[bytes]
    url: str
    log: Path

    def stop(self) -> int:
        """SIGTERM and the exit status, which is the signal itself once uvicorn shut down in order;
        a pod that does not leave is killed and fails the test."""
        self.process.send_signal(signal.SIGTERM)
        try:
            return self.process.wait(timeout=_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get("AGENTS_TEST_REDIS_URL")
    if url is None:
        pytest.fail("AGENTS_TEST_REDIS_URL is unset; run scripts/run_redis_tests.sh")
    return url


@pytest.fixture
def admin(redis_url: str) -> Iterator[Redis]:
    """The runner's own client: a different user from either pod."""
    client = Redis.from_url(redis_url, decode_responses=True)
    client.flushall()
    yield client
    client.acl_deluser(_POD_A_USER)
    client.flushall()
    client.close()


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
                "tests.integration.real_redis.pod",
                str(port),
                redis_url,
                _PREFIX,
            ],
            cwd=_REPOSITORY,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    pod = Pod(process, f"http://127.0.0.1:{port}", log)
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text()
        with suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
            return pod
        time.sleep(0.1)
    process.kill()
    pytest.fail(f"the pod on port {port} was not ready: {log.read_text()}")


@pytest.fixture
def pods(tmp_path: Path, redis_url: str, admin: Redis) -> Iterator[tuple[Pod, Pod]]:
    """Pod A as its own Redis user, pod B as the default one."""
    admin.acl_setuser(
        _POD_A_USER,
        enabled=True,
        passwords=[f"+{_POD_A_PASSWORD}"],
        keys=["*"],
        channels=["*"],
        commands=["+@all"],
    )
    pod_a = started(tmp_path, as_user(redis_url, _POD_A_USER, _POD_A_PASSWORD))
    pod_b = started(tmp_path, redis_url)
    yield pod_a, pod_b
    for pod in (pod_a, pod_b):
        if pod.process.poll() is None:
            pod.process.kill()
        pod.process.wait()


def request_of(body: bytes) -> tuple[bytes, dict[str, str]]:
    scope = member_scope(WRITTEN_MEMBER, body)
    return body, {name.decode(): value.decode() for name, value in scope["headers"]}


class Frames:
    """The JSON-RPC frames of one listen stream, read one at a time."""

    def __init__(self, response: httpx2.Response) -> None:
        self._lines = response.iter_lines()

    def next(self) -> dict[str, Any]:
        deadline = time.monotonic() + _WAIT_SECONDS
        for line in self._lines:
            if line.startswith("data: "):
                return json.loads(line.removeprefix("data: "))
            assert time.monotonic() < deadline, "no frame arrived"
        pytest.fail("the stream ended without a frame")

    def next_method(self) -> str | None:
        return self.next().get("method")


@contextmanager
def listening_on(pod: Pod) -> Iterator[Frames]:
    body, headers = request_of(
        rpc_body(
            "subscriptions/listen", {"notifications": {"resourceSubscriptions": [STANDING_URI]}}
        )
    )
    with (
        httpx2.Client(timeout=httpx2.Timeout(_WAIT_SECONDS, read=None), trust_env=False) as http,
        http.stream("POST", pod.url + MCP_ENDPOINT_PATH, content=body, headers=headers) as response,
    ):
        yield Frames(response)


def write_on(pod: Pod) -> int:
    return httpx2.post(pod.url + WRITE_PATH, timeout=_WAIT_SECONDS, trust_env=False).json()[
        "writes"
    ]


def status_of_a_request_to(pod: Pod) -> int:
    body, headers = request_of(rpc_body("tools/list"))
    answer = httpx2.post(
        pod.url + MCP_ENDPOINT_PATH,
        content=body,
        headers=headers,
        timeout=_WAIT_SECONDS,
        trust_env=False,
    )
    return answer.status_code


def test_a_write_on_one_pod_wakes_a_listen_on_the_other(pods: tuple[Pod, Pod]) -> None:
    pod_a, pod_b = pods

    with listening_on(pod_a) as frames:
        assert frames.next_method() == _ACKNOWLEDGED
        write_on(pod_b)

        assert frames.next_method() == _UPDATED


def test_the_pods_wake_each_other_on_agents_own_channel_and_not_the_hosts(
    pods: tuple[Pod, Pod], admin: Redis
) -> None:
    channels = admin.pubsub_channels()

    assert channels == [f"{_PREFIX}-agents:tandem:v1:wake"]


def test_a_subscription_that_is_cut_resyncs_the_open_listen(
    pods: tuple[Pod, Pod], admin: Redis
) -> None:
    pod_a, _pod_b = pods

    with listening_on(pod_a) as frames:
        assert frames.next_method() == _ACKNOWLEDGED
        admin.client_kill_filter(user=_POD_A_USER, _type="pubsub")

        assert frames.next_method() == _UPDATED


def test_after_a_pod_stops_the_other_pod_serves_the_write_the_store_holds(
    pods: tuple[Pod, Pod], admin: Redis
) -> None:
    pod_a, pod_b = pods

    assert pod_a.stop() == -signal.SIGTERM
    written = write_on(pod_b)

    assert admin.get(WRITES_KEY) == str(written)
    assert status_of_a_request_to(pod_b) == 200


def test_a_subscription_redis_refuses_ends_the_listen_and_answers_503_while_the_other_pod_serves(
    pods: tuple[Pod, Pod], admin: Redis
) -> None:
    pod_a, pod_b = pods

    with listening_on(pod_a) as frames:
        assert frames.next_method() == _ACKNOWLEDGED
        admin.acl_setuser(_POD_A_USER, reset_channels=True)
        admin.client_kill_filter(user=_POD_A_USER, _type="pubsub")

        assert "result" in frames.next()

    assert status_of_a_request_to(pod_a) == 503
    assert status_of_a_request_to(pod_b) == 200
    assert write_on(pod_b) == 1
