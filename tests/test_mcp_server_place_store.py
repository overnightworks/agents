from __future__ import annotations

import pytest

from agents.mcp_server.place_store import PlaceStoreError, failure_type


def a_store_error_caused_by(*causes: Exception) -> PlaceStoreError:
    failure = PlaceStoreError("the secret detail")
    link: Exception = failure
    for cause in causes:
        link.__cause__ = cause
        link = cause
    return failure


def a_runtime_error_caused_by(cause: Exception) -> RuntimeError:
    failure = RuntimeError("outer")
    failure.__cause__ = cause
    return failure


@pytest.mark.parametrize(
    ("failure", "logged_type"),
    [
        pytest.param(a_store_error_caused_by(), "PlaceStoreError", id="a store error alone"),
        pytest.param(a_store_error_caused_by(PermissionError()), "PermissionError", id="one cause"),
        pytest.param(
            a_store_error_caused_by(RuntimeError(), ConnectionError()),
            "ConnectionError",
            id="the innermost of a chain",
        ),
        pytest.param(RuntimeError("the secret detail"), "RuntimeError", id="another failure"),
        pytest.param(
            a_runtime_error_caused_by(ConnectionError()),
            "RuntimeError",
            id="another failure with a cause",
        ),
    ],
)
def test_failure_type_names_the_innermost_cause_of_a_store_error_and_never_its_text(
    failure: Exception, logged_type: str
) -> None:
    assert failure_type(failure) == logged_type
