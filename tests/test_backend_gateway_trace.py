"""``GatewayBackend`` and the model list send the host's trace to the gateway on every request."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from logline import current_traceparent, unit_trace

from agents.backend.fake_gateway import FakeGateway, Reply, completion, model_list, picture
from agents.backend.gateway import GatewayBackend, list_gateway_models
from tests.support.gateway_turn_support import Scenario, a_picture_request, scenario

scenario = scenario


def _completions(scenario: Scenario, gateway: FakeGateway) -> None:
    scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))


def _pictures(scenario: Scenario, gateway: FakeGateway) -> None:
    backend = GatewayBackend(scenario.config(), transport=gateway)
    scenario.outcome(backend, a_picture_request())


def _model_list(scenario: Scenario, gateway: FakeGateway) -> None:
    asyncio.run(list_gateway_models(scenario.config().gateway, transport=gateway))


REQUESTS: list[tuple[Callable[[Scenario, FakeGateway], None], Reply]] = [
    (_completions, completion("hi")),
    (_pictures, picture(b"PNG")),
    (_model_list, model_list("text/a")),
]


@pytest.mark.parametrize(("send", "reply"), REQUESTS, ids=["completions", "pictures", "models"])
def test_a_request_inside_a_trace_carries_the_traces_id(scenario, send, reply):
    gateway = FakeGateway(reply)

    with unit_trace(None):
        trace_id = (current_traceparent() or "").split("-")[1]
        send(scenario, gateway)

    (request,) = gateway.requests
    assert request.headers["traceparent"].split("-")[1] == trace_id


@pytest.mark.parametrize(("send", "reply"), REQUESTS, ids=["completions", "pictures", "models"])
def test_a_request_outside_a_trace_carries_no_traceparent(scenario, send, reply):
    gateway = FakeGateway(reply)

    send(scenario, gateway)

    (request,) = gateway.requests
    assert "traceparent" not in request.headers
