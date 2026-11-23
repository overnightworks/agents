"""``GatewayBackend`` holding a reply to one tool call when the request asks for it."""

from __future__ import annotations

import pytest

from agents.backend.fake_gateway import completion, tool_call, tool_round
from agents.turn_events import ToolCallEvent, ToolResultEvent
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import sent

shop = gateway_turn_support.shop

NOT_RUN = "not run: one call per reply"


def test_a_spoken_and_a_written_request_of_one_agent_send_the_same_first_body(shop):
    shop.add_tool("lookup")

    _, _, written = shop.run(completion("ok"))
    _, _, spoken = shop.run(completion("ok"), one_call_per_reply=True)

    assert written.requests[0].content == spoken.requests[0].content
    assert "parallel_tool_calls" not in sent(spoken, 0)


@pytest.mark.parametrize("extra", [1, 2], ids=["two-calls", "three-calls"])
def test_a_reply_with_several_calls_serves_the_first_and_tells_the_model_of_the_rest(shop, extra):
    shop.add_tool("lookup")
    calls = [tool_call(f"c{number}", "lookup", {}) for number in range(1, extra + 2)]

    events, stopped, gateway = shop.run(
        tool_round(*calls), completion("done"), one_call_per_reply=True
    )

    assert stopped is None
    assert [(type(event), event.tool_use_id) for event in events[:2]] == [
        (ToolCallEvent, "c1"),
        (ToolResultEvent, "c1"),
    ]
    assert len(shop.seen) == 1
    tool_messages = [m for m in sent(gateway, 1)["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == [call["id"] for call in calls]
    assert tool_messages[0]["content"] == "noted"
    assert all(NOT_RUN in m["content"] for m in tool_messages[1:])


def test_the_unserved_calls_do_not_count_against_the_call_bound(shop):
    shop.add_tool("lookup")

    _, stopped, _ = shop.run(
        tool_round(tool_call("c1", "lookup", {}), tool_call("c2", "lookup", {})),
        completion("done"),
        max_tool_calls=1,
        one_call_per_reply=True,
    )

    assert stopped is None
    assert len(shop.seen) == 1


def test_a_spoken_reply_with_the_same_id_twice_is_refused(shop):
    shop.add_tool("lookup")

    _, stopped, _ = shop.run(
        tool_round(tool_call("c1", "lookup", {}), tool_call("c1", "lookup", {})),
        completion("never"),
        one_call_per_reply=True,
    )

    assert stopped is not None
    assert not shop.seen


def test_with_the_setting_off_every_call_of_a_reply_is_served(shop):
    shop.add_tool("lookup")

    _, stopped, _ = shop.run(
        tool_round(tool_call("c1", "lookup", {}), tool_call("c2", "lookup", {})),
        completion("done"),
    )

    assert stopped is None
    assert len(shop.seen) == 2
