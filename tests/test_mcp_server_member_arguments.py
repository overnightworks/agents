"""The person front answers a member tool call whose arguments do not fit the tool's record as a
tool error naming the failing fields, and never runs the handler."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents.mcp_server import McpPersonFront
from agents.member import (
    ACCEPT_INVITATION_TOOL,
    CONTINUE_SPEAKING_TOOL,
    DECLINE_INVITATION_TOOL,
    GESTURE_TOOL,
    SET_TURN_STATE_TOOL,
    SPEAK_TOOL,
    STOP_SPEAKING_TOOL,
)
from tests.support.log_lines import LogLines, log_lines
from tests.support.mcp_person_support import Person, call_tool
from tests.support.member_wire import (
    accept_wire,
    continue_wire,
    decline_wire,
    gesture_wire,
    speak_wire,
    stop_wire,
    turn_state_wire,
)

log_lines = log_lines

_REFUSED = "the arguments do not fit the tool: "
_FIGURE = {"url": "https://figure.test/f.vrm", "sha256": "0" * 64}
_MEMBER_TOOLS = (
    ACCEPT_INVITATION_TOOL,
    DECLINE_INVITATION_TOOL,
    SPEAK_TOOL,
    STOP_SPEAKING_TOOL,
    CONTINUE_SPEAKING_TOOL,
    GESTURE_TOOL,
    SET_TURN_STATE_TOOL,
)


@pytest.fixture
def person() -> Person:
    return Person()


@pytest.fixture
def front(person: Person) -> McpPersonFront:
    return McpPersonFront(
        [person.tool(name) for name in (*_MEMBER_TOOLS, "read_note")],
        resolve_person=person.resolve,
    )


@pytest.mark.parametrize(
    ("tool", "arguments", "failing"),
    [
        (ACCEPT_INVITATION_TOOL, accept_wire(epoch=-1), "epoch (greater_than_equal)"),
        (DECLINE_INVITATION_TOOL, decline_wire(reason="later"), "reason (enum)"),
        (SPEAK_TOOL, speak_wire(text=5), "text (string_type)"),
        (STOP_SPEAKING_TOOL, {}, "place (missing)"),
        (CONTINUE_SPEAKING_TOOL, continue_wire(interrupted=""), "interrupted (too_short)"),
        (GESTURE_TOOL, gesture_wire(gesture="cartwheel"), "gesture (enum)"),
        (SET_TURN_STATE_TOOL, turn_state_wire(stopped="whatever"), "stopped (enum)"),
    ],
)
def test_a_member_tool_call_that_does_not_fit_its_record_is_a_tool_error_and_never_runs_the_handler(
    front: McpPersonFront,
    person: Person,
    log_lines: LogLines,
    tool: str,
    arguments: dict[str, Any],
    failing: str,
) -> None:
    result = asyncio.run(call_tool(front, tool, arguments))

    (block,) = result.content
    assert (result.is_error, block.text) == (True, _REFUSED + failing)
    assert person.calls == []
    ended = log_lines.one("tool_call.ended")
    assert (ended["tool_name"], ended["outcome"], ended["reason"]) == (
        tool,
        "refused",
        "tool_error",
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ACCEPT_INVITATION_TOOL, accept_wire(voices=["en-a", "de-b"], figure=_FIGURE)),
        (
            ACCEPT_INVITATION_TOOL,
            accept_wire(figure=_FIGURE | {"gestures": {"wave": _FIGURE}}),
        ),
        (
            ACCEPT_INVITATION_TOOL,
            accept_wire(
                looks=[{"kind": "video", "name": "studio-1"}, {"kind": "figure"} | _FIGURE]
            ),
        ),
        (DECLINE_INVITATION_TOOL, decline_wire()),
        (SPEAK_TOOL, speak_wire(mood="happy")),
        (STOP_SPEAKING_TOOL, stop_wire()),
        (CONTINUE_SPEAKING_TOOL, continue_wire()),
        (GESTURE_TOOL, gesture_wire()),
        (SET_TURN_STATE_TOOL, turn_state_wire()),
        (SET_TURN_STATE_TOOL, turn_state_wire(stopped=None)),
    ],
)
def test_a_member_tool_call_that_fits_its_record_reaches_the_handler_as_sent(
    front: McpPersonFront, person: Person, tool: str, arguments: dict[str, Any]
) -> None:
    result = asyncio.run(call_tool(front, tool, arguments))

    assert result.is_error is not True
    assert [(name, sent) for name, _request, sent in person.calls] == [(tool, arguments)]


@pytest.mark.parametrize(
    ("arguments", "failing"),
    [
        pytest.param(
            accept_wire(
                figure=_FIGURE | {"gestures": {"wave": _FIGURE | {"url": "http://a.test"}}}
            ),
            "figure.gestures.wave.url (value_error)",
            id="a key that is a word of the gesture vocabulary is written out",
        ),
        pytest.param(
            accept_wire(figure=_FIGURE | {"gestures": {"cartwheel-secret": _FIGURE}}),
            "figure.gestures.[key] (enum)",
            id="a key outside the vocabulary is named as a key",
        ),
        pytest.param(
            accept_wire(
                figure=_FIGURE
                | {"gestures": {"cartwheel-secret": _FIGURE | {"url": "http://a.test"}}}
            ),
            "figure.gestures.[key] (enum), figure.gestures.[key].url (value_error)",
            id="a caller key that is no vocabulary word stays a key under its entry too",
        ),
        pytest.param(
            accept_wire(figure=_FIGURE | {"gestures": {"url": _FIGURE}}),
            "figure.gestures.[key] (enum)",
            id="a key spelled like a field of the value model is still named as a key",
        ),
        pytest.param(
            accept_wire(looks=[{"kind": "video", "name": "a"}, {"kind": "video", "name": "a"}]),
            "looks (value_error)",
            id="a video look named twice is refused",
        ),
        pytest.param(
            accept_wire(looks=[{"kind": "figure"} | _FIGURE | {"url": "http://a.test"}]),
            "looks.0.[key].url (value_error)",
            id="the kind tag of a failing look is named as a key",
        ),
        pytest.param(
            accept_wire(looks=[{"kind": "hologram-secret"}]),
            "looks.0 (union_tag_invalid)",
            id="a kind outside the union is named at its look",
        ),
        pytest.param(
            accept_wire(**{"unknown-secret": 1}),
            "[key] (extra_forbidden)",
            id="a field the record does not have is named as a key",
        ),
        pytest.param(
            accept_wire(voices=["en-a", "en-a"]),
            "voices (value_error)",
            id="the voices list parses from JSON and a repeated voice is refused",
        ),
        pytest.param(
            accept_wire(voices=[1, 2]),
            "voices.0 (string_type), voices.1 (string_type)",
            id="a list whose items all fail names the items and no length failure",
        ),
        pytest.param(
            accept_wire(voices=[]),
            "voices (too_short)",
            id="a list that is really too short names its length",
        ),
        pytest.param(
            accept_wire(
                epoch="bad-value",
                figure=_FIGURE | {"gestures": {"first-secret": _FIGURE, "second-secret": _FIGURE}},
            ),
            "epoch (int_type), figure.gestures.[key] (enum)",
            id="every failing path once, sorted",
        ),
    ],
)
def test_the_tool_error_names_paths_and_error_types_and_never_an_input_value(
    front: McpPersonFront, person: Person, arguments: dict[str, Any], failing: str
) -> None:
    result = asyncio.run(call_tool(front, ACCEPT_INVITATION_TOOL, arguments))

    (block,) = result.content
    assert (result.is_error, block.text) == (True, _REFUSED + failing)
    assert person.calls == []


def test_a_tool_that_is_no_member_tool_is_not_checked_by_the_front(
    front: McpPersonFront, person: Person
) -> None:
    asyncio.run(call_tool(front, "read_note", accept_wire(epoch=-1)))

    assert [(name, sent) for name, _request, sent in person.calls] == [
        ("read_note", accept_wire(epoch=-1))
    ]
