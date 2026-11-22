"""The ``language`` of a place's line: the code a spoken line was heard in, if the app names it."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.member import PlaceEntry
from tests.support.voice_lines import a_line, read_back_over_the_wire


@pytest.mark.parametrize("own", [False, True])
def test_a_spoken_line_survives_the_wire_with_its_language(own: bool) -> None:
    line = a_line(1, "Guten Tag", own=own, language="de")

    assert read_back_over_the_wire(line).language == line.language


@pytest.mark.parametrize("spoken", [False, True])
def test_a_line_without_a_language_dumps_without_the_key(spoken: bool) -> None:
    dumped = json.loads(a_line(1, "Ada says", spoken=spoken).model_dump_json())

    assert "language" not in dumped


def test_a_written_line_with_a_language_is_refused() -> None:
    marked = {**a_line(1, "Ada writes", spoken=False).model_dump(), "language": "de"}

    with pytest.raises(ValidationError):
        PlaceEntry.model_validate(marked)
