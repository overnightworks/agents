"""``GatewayBackend`` sends the host's trace to the gateway on every request."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from logline import current_traceparent, unit_trace
from logline.testing import configured

from agents.backend.fake_gateway import FakeGateway, Reply, completion, picture
from agents.backend.gateway import GatewayBackend
from tests.support.gateway_turn_support import Scenario, a_picture_request, scenario

scenario = scenario


@pytest.fixture(autouse=True)
def _host_configured():
    with configured("agents-tests"):
        yield


def _completions(scenario: Scenario, gateway: FakeGateway) -> None:
    scenario.outcome(GatewayBackend(scenario.config(), transport=gateway))


def _pictures(scenario: Scenario, gateway: FakeGateway) -> None:
    backend = GatewayBackend(scenario.config(), transport=gateway)
    scenario.outcome(backend, a_picture_request())


REQUESTS: list[tuple[Callable[[Scenario, FakeGateway], None], Reply]] = [
    (_completions, completion("hi")),
    (_pictures, picture(b"PNG")),
]


@pytest.mark.parametrize(("send", "reply"), REQUESTS, ids=["completions", "pictures"])
def test_a_request_inside_a_trace_carries_the_traces_id(scenario, send, reply):
    gateway = FakeGateway(reply)

    with unit_trace(None):
        trace_id = (current_traceparent() or "").split("-")[1]
        send(scenario, gateway)

    (request,) = gateway.requests
    assert request.headers["traceparent"].split("-")[1] == trace_id


@pytest.mark.parametrize(("send", "reply"), REQUESTS, ids=["completions", "pictures"])
def test_a_request_outside_a_trace_carries_no_traceparent(scenario, send, reply):
    gateway = FakeGateway(reply)

    send(scenario, gateway)

    (request,) = gateway.requests
    assert "traceparent" not in request.headers
