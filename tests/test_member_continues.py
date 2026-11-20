"""The ``continues`` marker of a place's line: a spoken line whose speaker's next sentence waits."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.member import PlaceEntry
from tests.support.voice_lines import a_line, read_back_over_the_wire


def a_continuing_line() -> PlaceEntry:
    return PlaceEntry.model_validate(
        {**a_line(1, "Ada begins", own=False).model_dump(), "continues": True}
    )


def test_a_continuing_line_survives_the_wire_with_its_marker() -> None:
    assert read_back_over_the_wire(a_continuing_line()).continues is True


@pytest.mark.parametrize("spoken", [False, True])
def test_a_line_that_does_not_continue_dumps_without_the_key(spoken: bool) -> None:
    dumped = json.loads(a_line(1, "Ada says", spoken=spoken).model_dump_json())

    assert "continues" not in dumped


@pytest.mark.parametrize("refused_with", [{"spoken": False}, {"cut": True}], ids=["written", "cut"])
def test_a_continuing_line_that_is_written_or_cut_is_refused(refused_with: dict[str, bool]) -> None:
    marked = {**a_continuing_line().model_dump(), **refused_with}

    with pytest.raises(ValidationError):
        PlaceEntry.model_validate(marked)
