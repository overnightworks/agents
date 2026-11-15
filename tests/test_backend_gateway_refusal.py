"""The gateway backend's reading of a refusal: a spent budget, a busy gateway and any other."""

from __future__ import annotations

import json

import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, Reply
from agents.backend.gateway import GatewayBackend
from agents.turn import MAX_EVENT_BYTES, RejectionCode
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import UNAVAILABLE, a_picture_request, ending

scenario = gateway_turn_support.scenario


def _error_body(**fields: str) -> bytes:
    return json.dumps({"error": {"message": "hash 7f3a and key ending ab12", **fields}}).encode()


BUDGET_SPENT = (TurnStopKind.REFUSED, RejectionCode.BUDGET_SPENT)
BUSY = (TurnStopKind.REFUSED, RejectionCode.BUSY)


@pytest.mark.parametrize(
    ("status", "body", "expected", "detail"),
    [
        (422, _error_body(type="budget_exceeded", code="422"), BUDGET_SPENT, None),
        (
            422,
            b'{"error":{"type":"budget_exceeded","code":"422","message":"person"}}',
            BUDGET_SPENT,
            None,
        ),
        (
            422,
            _error_body(type="invalid_request_error", code="422"),
            UNAVAILABLE,
            "gateway_refused",
        ),
        (
            422,
            _error_body(
                type="invalid_request_error",
                code="422",
                message="Budget has been exceeded! Current cost: 5.1, Max budget: 5.0",
            ),
            UNAVAILABLE,
            "gateway_refused",
        ),
        (422, _error_body(type="budget_exceeded", code="4220"), UNAVAILABLE, "gateway_refused"),
        (422, _error_body(type="budget_exceeded"), UNAVAILABLE, "gateway_refused"),
        (422, b"<html>budget_exceeded</html>", UNAVAILABLE, "gateway_refused"),
        (
            422,
            _error_body(type="budget_exceeded", code="422") + b" " * MAX_EVENT_BYTES,
            UNAVAILABLE,
            "gateway_refused",
        ),
        (400, _error_body(type="budget_exceeded", code="400"), UNAVAILABLE, "gateway_refused"),
        (429, _error_body(type="budget_exceeded", code="429"), BUSY, None),
        (500, _error_body(type="budget_exceeded", code="500"), UNAVAILABLE, "gateway_failed"),
    ],
    ids=[
        "spent-app-budget",
        "spent-person-budget",
        "another-422",
        "422-whose-message-names-a-budget",
        "budget-type-without-a-valid-code",
        "budget-type-without-a-code",
        "422-without-error-object",
        "422-over-the-read-bound",
        "another-4xx-with-the-type",
        "429-stays-busy",
        "5xx-stays-unavailable",
    ],
)
@pytest.mark.parametrize("picture", [False, True], ids=["text", "picture"])
def test_only_a_422_with_the_budget_error_type_is_a_spent_budget(
    scenario, status, body, expected, detail, picture
):
    request = a_picture_request() if picture else None

    _, stopped = scenario.outcome(
        GatewayBackend(scenario.config(), transport=FakeGateway(Reply(status, body=body))),
        request,
    )

    assert (ending(stopped), stopped.detail) == (expected, detail)
