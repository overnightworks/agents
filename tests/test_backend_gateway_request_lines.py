"""Every gateway request writes one logline line: how it ended, and never what it carried."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass

import httpx2
import pytest

from agents.backend import TurnStopped
from agents.backend.fake_gateway import (
    FakeGateway,
    Reply,
    completion,
    model_list,
    picture,
    tool_call,
    tool_round,
)
from agents.backend.gateway import GatewayBackend, ModelListUnavailable, list_gateway_models
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import (
    FailingTransport,
    HangingTransport,
    Scenario,
    Streamed,
    _turn,
    a_chunk,
    a_fragment,
    a_picture_request,
    a_request,
    an_event_stream,
)
from tests.support.log_lines import LogLine, LogLines, log_lines
from tests.support.virtual_time import VirtualTimeLoop

scenario = gateway_turn_support.scenario
shop = gateway_turn_support.shop
log_lines = log_lines

MODEL_REQUEST = "model_request.ended"
PICTURE_REQUEST = "picture_request.ended"
MODEL_LIST_REQUEST = "model_list_request.ended"


def _send_completion(scenario: Scenario, transport: httpx2.AsyncBaseTransport) -> None:
    backend = GatewayBackend(scenario.config(), transport=transport)
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        runner.run(_turn(backend, a_request(), scenario.person()[0]))


def _send_picture(scenario: Scenario, transport: httpx2.AsyncBaseTransport) -> None:
    backend = GatewayBackend(scenario.config(), transport=transport)
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        runner.run(_turn(backend, a_picture_request(), scenario.person()[0]))


def _send_model_list(scenario: Scenario, transport: httpx2.AsyncBaseTransport) -> None:
    with (
        suppress(ModelListUnavailable),
        asyncio.Runner(loop_factory=VirtualTimeLoop) as runner,
    ):
        runner.run(list_gateway_models(scenario.config().gateway, transport=transport))


type Send = Callable[[Scenario, httpx2.AsyncBaseTransport], None]


@dataclass(frozen=True)
class Request:
    """One kind of gateway request: how it is sent, its line, and how its deadline ends it."""

    send: Send
    event: str
    ok: Reply
    deadline_reason: str
    seconds_past_the_deadline: int


REQUESTS = [
    Request(_send_completion, MODEL_REQUEST, completion("hi"), "limit_exceeded", 31),
    Request(_send_picture, PICTURE_REQUEST, picture(b"PNG"), "limit_exceeded", 31),
    Request(_send_model_list, MODEL_LIST_REQUEST, model_list("text/a"), "timed_out", 11),
]
EVERY_REQUEST = pytest.mark.parametrize(
    "request_kind", REQUESTS, ids=["completions", "pictures", "models"]
)

BUDGET_BODY = json.dumps({"error": {"type": "budget_exceeded", "code": "422"}}).encode()
OTHER_422_BODY = json.dumps({"error": {"type": "invalid_request_error", "code": "422"}}).encode()


def _the_line(lines: LogLines, request_kind: Request) -> LogLine:
    return lines.one(request_kind.event)


@EVERY_REQUEST
def test_an_answered_request_is_one_ok_line_with_its_status(scenario, log_lines, request_kind):
    request_kind.send(scenario, FakeGateway(request_kind.ok))

    line = _the_line(log_lines, request_kind)
    assert (line["outcome"], line["status"], line["level"]) == ("ok", 200, "info")
    assert "reason" not in line


@EVERY_REQUEST
@pytest.mark.parametrize(
    ("reply", "outcome", "reason", "level"),
    [
        (Reply(429, {"Retry-After": "7"}), "refused", "busy", "info"),
        (Reply(422, body=BUDGET_BODY), "refused", "budget_spent", "info"),
        (Reply(422, body=OTHER_422_BODY), "failed", "gateway_refused", "warning"),
        (Reply(400), "failed", "gateway_refused", "warning"),
        (Reply(404), "failed", "gateway_refused", "warning"),
        (Reply(500), "failed", "gateway_failed", "warning"),
        (Reply(503), "failed", "gateway_failed", "warning"),
    ],
    ids=["429", "422-budget", "422-other", "400", "404", "500", "503"],
)
def test_a_status_the_gateway_answers_ends_the_line_as_its_table_says(
    scenario, log_lines, request_kind, reply, outcome, reason, level
):
    request_kind.send(scenario, FakeGateway(reply))

    line = _the_line(log_lines, request_kind)
    assert (line["outcome"], line["reason"], line["status"], line["level"]) == (
        outcome,
        reason,
        reply.status,
        level,
    )
    assert "error_type" not in line


@EVERY_REQUEST
@pytest.mark.parametrize("status", [400, 500], ids=["400", "500"])
def test_a_400_and_a_5xx_are_closed_without_their_body_read(
    scenario, log_lines, request_kind, status
):
    transport = Streamed((OTHER_422_BODY,), status=status)

    request_kind.send(scenario, transport)

    assert not transport.read


@EVERY_REQUEST
def test_a_lost_connection_is_a_failed_line_with_no_status(scenario, log_lines, request_kind):
    request_kind.send(scenario, FailingTransport())

    line = _the_line(log_lines, request_kind)
    assert (line["outcome"], line["reason"], line["level"]) == (
        "failed",
        "connection_failed",
        "warning",
    )
    assert "status" not in line


@EVERY_REQUEST
def test_a_request_past_its_deadline_is_a_failed_line_with_the_deadlines_reason(
    scenario, log_lines, request_kind
):
    slow = HangingTransport(seconds_passing=request_kind.seconds_past_the_deadline)

    request_kind.send(scenario, slow)

    line = _the_line(log_lines, request_kind)
    assert (line["outcome"], line["reason"], line["level"]) == (
        "failed",
        request_kind.deadline_reason,
        "warning",
    )


def test_a_round_line_carries_the_first_output_and_the_tokens_of_its_usage(scenario, log_lines):
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 9,
        "prompt_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 25},
    }

    _send_completion(scenario, FakeGateway(completion("Hel", "lo", usage=usage)))

    line = log_lines.one(MODEL_REQUEST)
    assert (
        line["input_tokens"],
        line["output_tokens"],
        line["cached_input_tokens"],
        line["cache_write_tokens"],
    ) == (120, 9, 80, 25)
    assert 0 <= float(str(line["first_output_ms"])) <= float(str(line["duration_ms"]))


def test_a_round_that_streams_no_text_has_no_first_output(shop, log_lines):
    shop.add_tool("lookup")

    shop.run(tool_round(tool_call("c1", "lookup", {})), completion("Done."))

    first, second = log_lines.of(MODEL_REQUEST)
    assert "first_output_ms" not in first
    assert "first_output_ms" in second


def test_each_round_of_a_turn_writes_its_own_line(shop, log_lines):
    shop.add_tool("lookup")

    shop.run(tool_round(tool_call("c1", "lookup", {})), completion("Done."))

    lines = log_lines.of(MODEL_REQUEST)
    assert [line["outcome"] for line in lines] == ["ok", "ok"]


def test_an_error_event_is_a_gateway_failed_line_that_carries_none_of_the_event(
    scenario, log_lines
):
    error = {"message": f"leaks {scenario.key}", "type": "api_error", "code": "500"}
    transport = Streamed((an_event_stream(a_chunk("a"), {"error": error}),))

    _send_completion(scenario, transport)

    line = log_lines.one(MODEL_REQUEST)
    assert (line["outcome"], line["reason"], line["status"]) == ("failed", "gateway_failed", 200)
    assert "api_error" not in log_lines.everything()
    assert "leaks" not in log_lines.everything()


def test_a_reply_that_is_no_round_is_a_malformed_reply_line(scenario, log_lines):
    transport = Streamed((an_event_stream("{not json marker-in-the-wire"),))

    _send_completion(scenario, transport)

    line = log_lines.one(MODEL_REQUEST)
    assert (line["outcome"], line["reason"]) == ("failed", "malformed_reply")
    assert "marker-in-the-wire" not in log_lines.everything()


@pytest.mark.parametrize(
    "stream",
    [
        an_event_stream(a_chunk("cut", finish_reason="length")),
        an_event_stream(
            a_chunk(tool_calls=[a_fragment(0, call_id="c0", name="not_offered", arguments="{}")]),
            a_chunk(finish_reason="tool_calls"),
        ),
    ],
    ids=["cut-short", "call-to-a-tool-not-offered"],
)
def test_a_reply_read_whole_that_is_no_valid_round_is_a_malformed_reply_line(
    scenario, log_lines, stream
):
    _send_completion(scenario, Streamed((stream,)))

    line = log_lines.one(MODEL_REQUEST)
    assert (line["outcome"], line["reason"]) == ("failed", "malformed_reply")


def test_a_deadline_reached_while_the_app_holds_a_piece_is_a_limit_exceeded_line(
    scenario, log_lines
):
    request = a_request()
    backend = GatewayBackend(
        scenario.config(),
        transport=Streamed((an_event_stream(a_chunk("a"), done=False),), hangs=True),
    )

    async def pull_late() -> None:
        async with aclosing(backend.run(request, scenario.person()[0])) as stream:
            await anext(stream)
            await asyncio.sleep(request.max_duration_seconds)
            with suppress(TurnStopped):
                await anext(stream)

    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        runner.run(pull_late())

    line = log_lines.one(MODEL_REQUEST)
    assert (line["outcome"], line["reason"]) == ("failed", "limit_exceeded")


def test_the_hosts_close_during_a_round_is_a_cancelled_line(scenario, log_lines):
    transport = Streamed((an_event_stream(a_chunk("a"), done=False),), hangs=True)
    backend = GatewayBackend(scenario.config(), transport=transport)

    scenario.outcome(backend, leaves_after=1)

    line = log_lines.one(MODEL_REQUEST)
    assert line["outcome"] == "cancelled"
    assert "reason" not in line
    assert "error_type" not in line


@EVERY_REQUEST
def test_no_line_holds_the_key_a_prompt_a_reply_or_a_body(scenario, log_lines, request_kind):
    body = b'{"error":{"message":"marker-in-the-wire"}}'

    request_kind.send(scenario, FakeGateway(request_kind.ok))
    request_kind.send(scenario, FakeGateway(Reply(500, body=body)))

    written = log_lines.everything()
    assert scenario.key not in written
    for marker in ("marker-in-the-wire", "Answer briefly.", "Hello", "a cat", "UE5H"):
        assert marker not in written
