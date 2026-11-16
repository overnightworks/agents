"""Two pods of one host on one real Redis: a write on one wakes a listen on the other, and what
one pod's listen records is listed and heard by the other. Beside them, the listen record driven
through the adapter in one process: the scripts on the real engine and a claim that runs out in
real seconds.

``scripts/run_redis_tests.sh`` runs this directory against a throwaway Redis and passes its URL in
``AGENTS_TEST_REDIS_URL``. Each pod is its own uvicorn process; pod A connects as its own Redis
user, so a test can take away what that user may do.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from logline.trace import current_trace_id
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from tandem import wake_channel
from tandem.keys import deadline_keys

from agents.mcp_server import ResourceChanges
from agents.mcp_server.listen import LAPSE_POLL_SECONDS
from agents.mcp_server.place_store import LapseRun, ListeningTurn, ResourceChange, Resubscribed
from agents.mcp_server.redis_place_store import CLAIM_FOR, RedisPlaceStore
from agents.mcp_wire import MCP_ENDPOINT_PATH
from agents.member import LISTEN_ALLOWANCE_SECONDS, LISTEN_BOUND_SECONDS, STANDING_URI
from agents.turn import Principal
from tests.support.faulty_redis import EvalshaStopsAfter
from tests.support.log_lines import LogLines, log_lines
from tests.support.mcp_server_support import a_member, member_scope, rpc_body
from tests.support.real_redis_pod import (
    LISTENERS_PATH,
    LISTENING_CHANGES_PATH,
    LISTENING_TRACES_PATH,
    WRITE_PATH,
    WRITES_KEY,
    WRITTEN_MEMBER,
)
from tests.support.real_redis_pods import POD_A_USER, PREFIX, WAIT_SECONDS, Pod, wait_until
from tests.support.redis_listen_support import (
    UNUSUAL_PRINCIPALS,
    UNUSUAL_URIS,
    delivered_up_to,
    eventually,
    namespace_of,
    snapshots_around_evalsha,
    subscribed,
)

log_lines = log_lines

_ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
_UPDATED = "notifications/resources/updated"
_YES = [WRITTEN_MEMBER.iss, WRITTEN_MEMBER.sub, STANDING_URI, True]
_NO = [WRITTEN_MEMBER.iss, WRITTEN_MEMBER.sub, STANDING_URI, False]
_OPEN = timedelta(seconds=LISTEN_BOUND_SECONDS + LISTEN_ALLOWANCE_SECONDS)
_FIRST_BARRIER = ResourceChange(a_member("bob"), STANDING_URI, "first")
_SECOND_BARRIER = ResourceChange(a_member("bob"), STANDING_URI, "second")
_SECONDS_PAST_THE_CLAIM = 10


def request_of(body: bytes, *, traceparent: str | None = None) -> tuple[bytes, dict[str, str]]:
    scope = member_scope(WRITTEN_MEMBER, body)
    headers = {name.decode(): value.decode() for name, value in scope["headers"]}
    if traceparent is not None:
        headers["traceparent"] = traceparent
    return body, headers


class Frames:
    """The JSON-RPC frames of one listen stream, read one at a time."""

    def __init__(self, response: httpx2.Response) -> None:
        self._lines = response.iter_lines()

    def next(self) -> dict[str, Any]:
        deadline = time.monotonic() + WAIT_SECONDS
        for line in self._lines:
            if line.startswith("data: "):
                return json.loads(line.removeprefix("data: "))
            assert time.monotonic() < deadline, "no frame arrived"
        pytest.fail("the stream ended without a frame")

    def next_method(self) -> str | None:
        return self.next().get("method")


@contextmanager
def listening_on(pod: Pod, *, traceparent: str | None = None) -> Iterator[Frames]:
    body, headers = request_of(
        rpc_body(
            "subscriptions/listen", {"notifications": {"resourceSubscriptions": [STANDING_URI]}}
        ),
        traceparent=traceparent,
    )
    with (
        httpx2.Client(timeout=httpx2.Timeout(WAIT_SECONDS, read=None), trust_env=False) as http,
        http.stream("POST", pod.url + MCP_ENDPOINT_PATH, content=body, headers=headers) as response,
    ):
        yield Frames(response)


def write_on(pod: Pod) -> int:
    return httpx2.post(pod.url + WRITE_PATH, timeout=WAIT_SECONDS, trust_env=False).json()["writes"]


def listeners_of(pod: Pod) -> list[list[str]]:
    return httpx2.get(pod.url + LISTENERS_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def listening_changes_heard_by(pod: Pod) -> list[list[str | bool]]:
    return httpx2.get(
        pod.url + LISTENING_CHANGES_PATH, timeout=WAIT_SECONDS, trust_env=False
    ).json()


def listening_traces_heard_by(pod: Pod) -> list[str | None]:
    return httpx2.get(pod.url + LISTENING_TRACES_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def stream_shortened_to_the_allowance(admin: Redis) -> bool:
    """Whether the one stream on record comes due within the allowance, which ``ended`` arms."""
    due = deadline_keys(f"{PREFIX}-agents", "listen").due
    seconds, microseconds = admin.time()
    now_ms = seconds * 1000 + microseconds // 1000
    waiting = admin.zrange(due, 0, 0, withscores=True)
    return bool(waiting) and waiting[0][1] - now_ms <= LISTEN_ALLOWANCE_SECONDS * 1000


def status_of_a_request_to(pod: Pod) -> int:
    body, headers = request_of(rpc_body("tools/list"))
    answer = httpx2.post(
        pod.url + MCP_ENDPOINT_PATH,
        content=body,
        headers=headers,
        timeout=WAIT_SECONDS,
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

    assert channels == [wake_channel(f"{PREFIX}-agents")]


def test_a_subscription_that_is_cut_resyncs_the_open_listen(
    pods: tuple[Pod, Pod], admin: Redis
) -> None:
    pod_a, _pod_b = pods

    with listening_on(pod_a) as frames:
        assert frames.next_method() == _ACKNOWLEDGED
        admin.client_kill_filter(user=POD_A_USER, _type="pubsub")

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
        admin.acl_setuser(POD_A_USER, reset_channels=True)
        admin.client_kill_filter(user=POD_A_USER, _type="pubsub")

        assert "result" in frames.next()

    assert status_of_a_request_to(pod_a) == 503
    assert status_of_a_request_to(pod_b) == 200
    assert write_on(pod_b) == 1


def test_a_listen_on_one_pod_is_listed_by_the_other_and_its_host_hears_the_yes(
    pods: tuple[Pod, Pod],
) -> None:
    pod_a, pod_b = pods

    with listening_on(pod_a) as frames:
        assert frames.next_method() == _ACKNOWLEDGED

        assert listeners_of(pod_b) == [[WRITTEN_MEMBER.iss, WRITTEN_MEMBER.sub]]
        wait_until(lambda: listening_changes_heard_by(pod_b) == [_YES], seconds=WAIT_SECONDS)


def test_a_listen_on_one_pod_reaches_the_host_of_the_other_in_the_trace_of_its_request(
    pods: tuple[Pod, Pod],
) -> None:
    pod_a, pod_b = pods
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

    with listening_on(pod_a, traceparent=f"00-{trace_id}-00f067aa0ba902b7-01") as frames:
        assert frames.next_method() == _ACKNOWLEDGED

        wait_until(lambda: listening_changes_heard_by(pod_b) == [_YES], seconds=WAIT_SECONDS)
        assert listening_traces_heard_by(pod_b) == [trace_id]


def test_after_the_runner_closed_its_stream_and_its_pod_was_killed_the_other_pod_hears_one_no(
    pods: tuple[Pod, Pod], admin: Redis
) -> None:
    pod_a, pod_b = pods

    with listening_on(pod_a) as frames:
        assert frames.next_method() == _ACKNOWLEDGED
        wait_until(lambda: listening_changes_heard_by(pod_b) == [_YES], seconds=WAIT_SECONDS)
    wait_until(lambda: stream_shortened_to_the_allowance(admin), seconds=WAIT_SECONDS)
    pod_a.kill()

    wait_until(
        lambda: listening_changes_heard_by(pod_b) == [_YES, _NO],
        seconds=LISTEN_ALLOWANCE_SECONDS + CLAIM_FOR.total_seconds() + LAPSE_POLL_SECONDS + 5,
    )
    time.sleep(2 * LAPSE_POLL_SECONDS)
    assert listening_changes_heard_by(pod_b) == [_YES, _NO]
    assert listeners_of(pod_b) == []


def a_store(redis_url: str) -> RedisPlaceStore:
    return RedisPlaceStore(AsyncRedis.from_url(redis_url), key_prefix=PREFIX)


def test_two_pods_following_one_store_write_one_opened_line_and_one_lapsed_line(
    redis_url: str, admin: Redis, log_lines: LogLines
) -> None:
    async def scenario() -> None:
        pod_a = ResourceChanges(a_store(redis_url))
        pod_b = ResourceChanges(a_store(redis_url))
        async with pod_a.following(), pod_b.following():
            async with pod_a.opened(WRITTEN_MEMBER) as stream:
                await pod_a.acknowledged(WRITTEN_MEMBER, stream, [STANDING_URI], tools=False)

            async def lapsed() -> bool:
                return bool(log_lines.of("listening.lapsed"))

            await eventually(
                lapsed,
                seconds=LISTEN_ALLOWANCE_SECONDS
                + CLAIM_FOR.total_seconds()
                + LAPSE_POLL_SECONDS
                + 5,
            )
            await asyncio.sleep(2 * LAPSE_POLL_SECONDS)

    asyncio.run(scenario())

    assert [line["sub"] for line in log_lines.of("listening.opened")] == [WRITTEN_MEMBER.sub]
    assert [line["sub"] for line in log_lines.of("listening.lapsed")] == [WRITTEN_MEMBER.sub]
    assert len(log_lines.of("listen_lapse.ended")) == 1


def test_the_no_a_lapse_run_wakes_is_heard_in_the_trace_of_that_run(
    redis_url: str, log_lines: LogLines
) -> None:
    traces_of_no: list[str | None] = []

    def heard(_principal: Principal, _uri: str, listens: bool) -> None:
        if not listens:
            traces_of_no.append(current_trace_id())

    async def scenario() -> None:
        pod_a = ResourceChanges(a_store(redis_url), listening_changed=heard)
        pod_b = ResourceChanges(a_store(redis_url), listening_changed=heard)
        async with pod_a.following(), pod_b.following():
            async with pod_a.opened(WRITTEN_MEMBER) as stream:
                await pod_a.acknowledged(WRITTEN_MEMBER, stream, [STANDING_URI], tools=False)
            await eventually(
                lambda: _true(bool(traces_of_no)),
                seconds=LISTEN_ALLOWANCE_SECONDS
                + CLAIM_FOR.total_seconds()
                + LAPSE_POLL_SECONDS
                + 5,
            )

    asyncio.run(scenario())

    run_trace = log_lines.one("listen_lapse.ended")["trace_id"]
    assert run_trace
    assert set(traces_of_no) == {run_trace}


async def _true(value: bool) -> bool:
    return value


def test_a_claim_its_pod_cannot_finish_is_lapsed_by_another_pod_once_the_claim_ran_out(
    redis_url: str, admin: Redis
) -> None:
    async def scenario() -> tuple[object, ...]:
        store_b = a_store(redis_url)
        store_a = RedisPlaceStore(
            EvalshaStopsAfter(redis_url, evalshas_that_return=1), key_prefix=PREFIX
        )
        async with subscribed(store_b) as events:
            await store_b.acknowledged("stream", WRITTEN_MEMBER, [STANDING_URI], _OPEN)
            await store_b.ended("stream", timedelta(0))
            await store_a.lapse_due("a")
            await store_b.lapse_due("b")
            listed_while_claimed = await store_b.listeners(STANDING_URI)
            before = await delivered_up_to(store_b, events, _FIRST_BARRIER)

            async def lapsed() -> bool:
                await store_b.lapse_due("b")
                return not await store_b.listeners(STANDING_URI)

            await eventually(lapsed, seconds=CLAIM_FOR.total_seconds() + _SECONDS_PAST_THE_CLAIM)
            return (
                listed_while_claimed,
                before,
                await delivered_up_to(store_b, events, _SECOND_BARRIER),
            )

    listed_while_claimed, before, after = asyncio.run(scenario())

    yes = ListeningTurn(WRITTEN_MEMBER, STANDING_URI, listens=True)
    no = ListeningTurn(WRITTEN_MEMBER, STANDING_URI, listens=False)
    assert listed_while_claimed == frozenset({WRITTEN_MEMBER})
    assert before == [Resubscribed(), yes, _FIRST_BARRIER]
    assert after == [Resubscribed(), yes, _FIRST_BARRIER, no, _SECOND_BARRIER]
    assert admin.keys("*") == []


@pytest.mark.parametrize("principal", UNUSUAL_PRINCIPALS)
@pytest.mark.parametrize("uri", UNUSUAL_URIS)
def test_a_lapsed_stream_leaves_no_member_whatever_its_principal_and_uri_hold(
    redis_url: str, admin: Redis, principal: Principal, uri: str
) -> None:
    async def scenario() -> tuple[list[object], bool, frozenset[Principal]]:
        store = a_store(redis_url)
        async with subscribed(store) as events:
            await store.acknowledged("stream", principal, [uri], timedelta(0))
            await store.lapse_due("pod")
            return (
                await delivered_up_to(store, events, _FIRST_BARRIER),
                await store.listens_to(principal, uri),
                await store.listeners(uri),
            )

    seen, listens, listeners = asyncio.run(scenario())

    assert seen == [
        Resubscribed(),
        ListeningTurn(principal, uri, listens=True),
        ListeningTurn(principal, uri, listens=False),
        _FIRST_BARRIER,
    ]
    assert (listens, listeners) == (False, frozenset())
    assert admin.keys("*") == []


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("no json", id="a record that is no json"),
        pytest.param(
            json.dumps(["no json", [STANDING_URI]]), id="a principal text that is no json"
        ),
    ],
)
def test_a_lapse_on_a_record_that_does_not_decode_writes_nothing_and_counts_the_failure(
    redis_url: str, admin: Redis, record: str
) -> None:
    """``cjson.decode`` raises on text that is no JSON; the fake Redis raises a Python error there
    that no real server shows."""
    alice = a_member("alice")

    async def scenario() -> tuple[tuple[object, object], list[object], LapseRun]:
        client = AsyncRedis.from_url(redis_url)
        watcher = AsyncRedis.from_url(redis_url)
        around_scripts = snapshots_around_evalsha(client, lambda: namespace_of(watcher, PREFIX))
        store = RedisPlaceStore(client, key_prefix=PREFIX)
        async with subscribed(store) as events:
            await store.acknowledged("held", alice, [STANDING_URI], timedelta(milliseconds=1))
            await delivered_up_to(store, events, _FIRST_BARRIER)
            admin.hset(f"{PREFIX}-agents:v1:streams", "held", record)

            runs: list[LapseRun] = []

            async def claimed() -> bool:
                runs.append(await store.lapse_due("pod"))
                return bool(
                    admin.zscore(deadline_keys(f"{PREFIX}-agents", "listen").claimed, "held")
                )

            await eventually(claimed, seconds=WAIT_SECONDS)
            return (
                around_scripts[-1],
                await delivered_up_to(store, events, _SECOND_BARRIER),
                runs[-1],
            )

    (before, after), seen, run = asyncio.run(scenario())

    assert after == before
    assert seen == [
        Resubscribed(),
        ListeningTurn(alice, STANDING_URI, listens=True),
        _FIRST_BARRIER,
        _SECOND_BARRIER,
    ]
    assert (run.turns, run.stream_count, run.failed_count) == ((), 0, 1)
