"""The failures a member call names its outcome by, and the codes it logs them under."""

from __future__ import annotations

import pytest

from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    LISTEN_NOT_HONORED_CODE,
    UNSUPPORTED_VERSION_CODE,
    CallFailure,
    StatusFailure,
    Unreachable,
    UnusableAnswer,
    jsonrpc_code,
    status_code,
)


@pytest.mark.parametrize(
    ("code", "written"),
    [
        pytest.param(INVALID_ANSWER_CODE, "invalid_answer", id="invalid answer"),
        pytest.param(LISTEN_NOT_HONORED_CODE, "listen_not_honored", id="listen not honored"),
        pytest.param(UNSUPPORTED_VERSION_CODE, "unsupported_version", id="unsupported version"),
        pytest.param(status_code(503), "http_503", id="status"),
        pytest.param(jsonrpc_code(-32602), "jsonrpc_-32602", id="jsonrpc"),
    ],
)
def test_a_code_is_written_as_the_runner_logs_it(code: str, written: str) -> None:
    assert code == written


def test_a_status_failure_says_its_status_method_and_answer() -> None:
    failure = StatusFailure(503, "tools/call", "text/html")

    assert (failure.status, failure.method, failure.answer) == (503, "tools/call", "text/html")


def test_an_unusable_answer_says_its_code_method_and_answer() -> None:
    failure = UnusableAnswer(INVALID_ANSWER_CODE, "resources/read", "jsonrpc_-32602")

    assert (failure.code, failure.method, failure.answer) == (
        INVALID_ANSWER_CODE,
        "resources/read",
        "jsonrpc_-32602",
    )
    assert str(failure) == INVALID_ANSWER_CODE


def test_a_failure_that_names_no_method_has_none() -> None:
    assert (Unreachable().method, Unreachable().answer) == (None, None)
    assert isinstance(Unreachable(), CallFailure)
