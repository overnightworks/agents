"""The failures a member call names its outcome by, and the codes it logs them under."""

from __future__ import annotations

import pytest

from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    LISTEN_NOT_HONORED_CODE,
    UNSUPPORTED_VERSION_CODE,
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
