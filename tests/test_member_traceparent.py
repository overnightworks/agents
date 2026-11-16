"""The ``traceparent`` of a place's line and view: the trace its waking change was made in."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from agents.member import PlaceEntry, PlaceView
from tests.support.member_wire import a_waking_trace, entry, window_from

MAX_TRACEPARENT_LENGTH = 256


def a_view_wire(**fields: Any) -> dict[str, Any]:
    return {"revision": 1, "awaits_you": False, "state": {}} | fields


RECORDS = [
    pytest.param(PlaceEntry, entry(1), id="line"),
    pytest.param(PlaceView, a_view_wire(), id="view"),
]


@pytest.mark.parametrize(("record", "wire"), RECORDS)
def test_a_traceparent_survives_the_wire(record: type[PlaceEntry | PlaceView], wire) -> None:
    traceparent = a_waking_trace().traceparent
    carrying = record.model_validate(wire | {"traceparent": traceparent})

    read = record.model_validate_json(carrying.model_dump_json())

    assert read.traceparent == traceparent


def test_a_line_carries_its_traceparent_through_a_window() -> None:
    traceparent = a_waking_trace().traceparent
    wire = {"generation": "g", "starts_at": 1, "entries": [entry(1, traceparent=traceparent)]}

    read = window_from(wire)

    assert read.entries[0].traceparent == traceparent


@pytest.mark.parametrize(("record", "wire"), RECORDS)
def test_a_record_without_a_traceparent_reads_as_before_and_dumps_without_the_key(
    record: type[PlaceEntry | PlaceView], wire
) -> None:
    read = record.model_validate(wire)

    assert read.traceparent is None
    assert "traceparent" not in json.loads(read.model_dump_json())


@pytest.mark.parametrize(("record", "wire"), RECORDS)
def test_a_value_that_is_no_trace_context_is_carried_up_to_the_bound(
    record: type[PlaceEntry | PlaceView], wire
) -> None:
    longest = "x" * MAX_TRACEPARENT_LENGTH

    assert record.model_validate(wire | {"traceparent": longest}).traceparent == longest


@pytest.mark.parametrize(("record", "wire"), RECORDS)
def test_a_traceparent_beyond_the_bound_is_refused(
    record: type[PlaceEntry | PlaceView], wire
) -> None:
    with pytest.raises(ValidationError):
        record.model_validate(wire | {"traceparent": "x" * (MAX_TRACEPARENT_LENGTH + 1)})
