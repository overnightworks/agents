"""The failures a member call names its outcome by, and the codes it logs them under."""

from __future__ import annotations

import dataclasses

import pytest

from agents.member_outcomes import (
    INVALID_ANSWER_CODE,
    LISTEN_NOT_HONORED_CODE,
    UNSUPPORTED_VERSION_CODE,
    ArgumentLeftOut,
    UnrecognisedRefusal,
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
        pytest.param(UnrecognisedRefusal.UNRECOGNISED.value, "unrecognised", id="unrecognised"),
    ],
)
def test_a_code_is_written_as_the_runner_logs_it(code: str, written: str) -> None:
    assert code == written


def test_a_left_out_argument_is_a_record_that_cannot_change() -> None:
    left_out = ArgumentLeftOut("speak", "style")

    with pytest.raises(dataclasses.FrozenInstanceError):
        left_out.argument = "other"  # type: ignore[misc]


def test_an_unusable_answer_names_the_entry_of_a_listing_only_when_given() -> None:
    assert (
        UnusableAnswer(INVALID_ANSWER_CODE).entry,
        UnusableAnswer(INVALID_ANSWER_CODE, entry=2).entry,
    ) == (
        None,
        2,
    )
