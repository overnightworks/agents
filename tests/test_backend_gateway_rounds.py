"""``GatewayBackend`` deferring tools, recording its tool rounds whole and replaying them."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, completion, tool_call, tool_round
from agents.tools import ToolDeclaration, ToolOutcome
from agents.turn import (
    MAX_EVENT_BYTES,
    TOOL_IMAGE_MEDIA_TYPE,
    FailureCode,
    ToolRound,
    Turn,
)
from agents.turn_events import FinalEvent, ToolResultEvent
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import (
    BAD_DEFERRALS,
    a_history,
    a_picture_request,
    a_request,
    a_round_call,
    a_tool_request,
    a_tool_round,
    answering,
    sent,
)
from tests.support.log_lines import log_lines
from tests.support.tool_images import a_tool_image

shop = gateway_turn_support.shop
log_lines = log_lines

MALFORMED = (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)
NOT_RUN = "not run: one call per reply"


def a_loader_shop(shop):
    """A shop with a loader tool that loads ``hidden``, and that one deferred tool."""
    shop.add_tool("loader", answer=answering(ToolOutcome("", is_error=False, loads=("hidden",))))
    shop.add_tool(
        "hidden", parameters={"type": "object", "properties": {"mode": {"type": "string"}}}
    )


def tool_messages(gateway, index: int) -> list[dict]:
    return [m for m in sent(gateway, index)["messages"] if m["role"] == "tool"]


def test_a_deferred_tool_is_sent_with_defer_loading_and_the_others_without(shop):
    for name in ("first", "second", "third"):
        shop.add_tool(name)

    _, _, deferring = shop.run(completion("ok"), deferred=("first", "third"))
    _, _, plain = shop.run(completion("ok"))

    entries = sent(deferring, 0)["tools"]
    assert [entry["function"]["name"] for entry in entries] == ["first", "second", "third"]
    assert [entry.get("defer_loading") for entry in entries] == [True, None, True]
    assert all("defer_loading" not in entry for entry in sent(plain, 0)["tools"])
    without = [{k: v for k, v in entry.items() if k != "defer_loading"} for entry in entries]
    assert without == sent(plain, 0)["tools"]


def test_a_request_without_tools_and_without_deferral_sends_no_tools_key(shop):
    _, stopped, gateway = shop.run(completion("ok"))

    assert stopped is None
    assert "tools" not in sent(gateway, 0)


@pytest.mark.parametrize("changes", BAD_DEFERRALS.values(), ids=BAD_DEFERRALS.keys())
def test_a_request_with_bad_deferral_is_refused_when_it_is_built(changes):
    tools = tuple(ToolDeclaration(name=name, description="d", parameters={}) for name in "ab")
    turn = {"turn": Turn(system="s", messages=changes["messages"])} if "messages" in changes else {}
    deferred = frozenset(changes["deferred"])

    with pytest.raises(ValueError):
        a_request(tools=tools, max_tool_calls=5, deferred_tools=deferred, **turn)


def test_a_picture_request_holding_a_tool_round_is_refused():
    turn = Turn(system="Paint it.", messages=[a_tool_round()])

    with pytest.raises(ValueError):
        a_picture_request(turn=turn)


@pytest.mark.parametrize("changes", BAD_DEFERRALS.values(), ids=BAD_DEFERRALS.keys())
def test_a_copied_request_that_breaks_a_rule_is_refused_by_the_run_before_anything_is_sent(
    shop, changes
):
    shop.add_tool("a")
    shop.add_tool("b")
    valid = a_tool_request(shop.tools, 5, deferred=("a",))
    update = {"deferred_tools": frozenset(changes["deferred"])}
    if "messages" in changes:
        update["turn"] = Turn(system="Answer briefly.", messages=changes["messages"])
    gateway = FakeGateway(completion("ok"))
    copied = valid.model_copy(update=update)

    with pytest.raises(ValueError):
        shop.run_request(copied, gateway)

    assert gateway.requests == []


def test_a_recorded_round_replays_as_one_call_message_and_a_tool_message_per_call(shop):
    shop.add_tool("lookup")
    history = a_history(
        a_tool_round(a_round_call("a", result="first"), a_round_call("b", result="second"))
    )

    _, stopped, gateway = shop.run(completion("ok"), history=history)

    assert stopped is None
    assert [m["role"] for m in sent(gateway, 0)["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "user",
    ]
    call_message, first, second = sent(gateway, 0)["messages"][2:5]
    assert call_message == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": id_, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
            for id_ in ("a", "b")
        ],
    }
    assert [(m["tool_call_id"], m["content"]) for m in (first, second)] == [
        ("a", "first"),
        ("b", "second"),
    ]


def test_a_recorded_round_keeps_its_text_and_arguments_as_recorded(shop):
    shop.add_tool("lookup")
    call = a_round_call(arguments='{"q": "\\u00e9"}')
    history = a_history(a_tool_round(call, text="Looking"))

    _, _, gateway = shop.run(completion("ok"), history=history)

    echoed = sent(gateway, 0)["messages"][2]
    assert echoed["content"] == "Looking"
    assert echoed["tool_calls"][0]["function"]["arguments"] == '{"q": "\\u00e9"}'


def test_a_turn_sends_and_records_the_arguments_as_the_model_spelled_them(shop):
    shop.add_tool("lookup")

    events, _, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", '{ "q" : "x" }')), completion("done")
    )

    (round_,) = events[-1].tool_rounds
    assert round_.calls[0].arguments == '{ "q" : "x" }'
    assert sent(gateway, 1)["messages"][-2]["tool_calls"][0]["function"]["arguments"] == (
        '{ "q" : "x" }'
    )


def test_an_error_answer_of_a_recorded_round_carries_the_error_prefix(shop):
    shop.add_tool("lookup")
    history = a_history(
        a_tool_round(a_round_call("a"), a_round_call("b", result=NOT_RUN, is_error=True))
    )

    _, _, gateway = shop.run(completion("ok"), history=history)

    assert tool_messages(gateway, 0)[1]["content"] == f"Error: {NOT_RUN}"


def test_an_image_answer_of_a_recorded_round_renders_as_image_then_text(shop):
    shop.add_tool("lookup")
    image = a_tool_image()
    history = a_history(a_tool_round(a_round_call(result="seen", image=image)))

    _, _, gateway = shop.run(completion("ok"), history=history)

    (parts,) = [m["content"] for m in tool_messages(gateway, 0)]
    assert [part["type"] for part in parts] == ["image_url", "text"]
    assert parts[0]["image_url"]["url"].startswith(f"data:{TOOL_IMAGE_MEDIA_TYPE};base64,")


@pytest.mark.parametrize("count", [1, 2, 37], ids=["one", "two", "thirty-seven"])
def test_a_loading_answer_renders_as_one_reference_per_name_in_order(shop, count):
    names = tuple(f"t{number}" for number in range(count))
    for name in names:
        shop.add_tool(name)
    shop.add_tool("other")
    history = a_history(a_tool_round(a_round_call(tool="other", result="", loads=names)))

    _, stopped, gateway = shop.run(completion("ok"), history=history, deferred=names)

    assert stopped is None
    (parts,) = [m["content"] for m in tool_messages(gateway, 0)]
    assert parts == [{"type": "tool_reference", "tool_name": name} for name in names]


@pytest.mark.parametrize(
    "changes",
    [
        {"loads": ("a",), "result": "text"},
        {"loads": ("a",), "image": a_tool_image()},
        {"loads": ("a",), "is_error": True},
        {"loads": ("a", "a")},
        {"call_id": ""},
        {"tool": ""},
        {"arguments": "[1]"},
        {"arguments": ""},
    ],
    ids=[
        "text",
        "image",
        "error",
        "repeated-names",
        "blank-id",
        "blank-tool",
        "arguments-no-object",
        "arguments-no-json",
    ],
)
def test_a_round_call_that_cannot_render_is_refused(changes):
    with pytest.raises(ValueError):
        a_round_call(**{"result": "", **changes})


@pytest.mark.parametrize(
    "build",
    [
        lambda: ToolRound(calls=()),
        lambda: ToolRound(text="", calls=(a_round_call(),)),
        lambda: Turn(
            system="s",
            messages=[a_tool_round(a_round_call("c1")), a_tool_round(a_round_call("c1"))],
        ),
        lambda: Turn(system="s", messages=[a_tool_round(a_round_call("c1"), a_round_call("c1"))]),
    ],
    ids=["no-calls", "empty-text", "id-shared-by-two-rounds", "id-named-twice-in-a-round"],
)
def test_a_round_or_a_turn_that_cannot_replay_is_refused(build):
    with pytest.raises(ValueError):
        build()


def test_a_reply_reusing_a_replayed_call_id_ends_the_round_failed_without_running_a_handler(
    shop,
):
    shop.add_tool("lookup")
    history = a_history(a_tool_round(a_round_call("c1")))

    events, stopped, _ = shop.run(
        tool_round(tool_call("c1", "lookup", {})), completion("never"), history=history
    )

    assert (stopped.kind, stopped.code, stopped.detail) == (*MALFORMED, "malformed_reply")
    assert not shop.seen
    assert events == []


def test_a_loading_answer_reaches_the_next_round_and_the_host(shop):
    a_loader_shop(shop)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "loader", {})),
        tool_round(tool_call("c2", "hidden", {"mode": "x"})),
        completion("done"),
        deferred=("hidden",),
    )

    assert stopped is None
    loaded = [e for e in events if isinstance(e, ToolResultEvent)][0]
    assert loaded.loads == ("hidden",)
    assert [m["content"] for m in tool_messages(gateway, 1)] == [
        [{"type": "tool_reference", "tool_name": "hidden"}]
    ]
    assert [seen[0] for seen in shop.seen] == ["loader", "hidden"]
    assert isinstance(events[-1], FinalEvent)


@pytest.mark.parametrize(
    "outcome",
    [
        ToolOutcome("", is_error=False, loads=("elsewhere",)),
        ToolOutcome("", is_error=False, loads=("loader",)),
        ToolOutcome("", is_error=True, loads=("hidden",)),
        ToolOutcome("", is_error=False, image=a_tool_image(), loads=("hidden",)),
        ToolOutcome("some text", is_error=False, loads=("hidden",)),
        ToolOutcome("", is_error=False, loads=("hidden", "hidden")),
    ],
    ids=["unknown", "not-deferred", "error", "image", "text", "repeated"],
)
def test_a_loading_answer_that_breaks_a_rule_is_a_failed_call_and_the_turn_goes_on(
    shop, log_lines, outcome
):
    shop.add_tool("loader", answer=answering(outcome))
    shop.add_tool("hidden")

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "loader", {})), completion("done"), deferred=("hidden",)
    )

    assert stopped is None
    assert log_lines.one("tool_call.ended")["outcome"] == "failed"
    assert events[1].loads == ()
    assert [m["content"] for m in tool_messages(gateway, 1)] == ["Error: The tool failed."]
    assert isinstance(events[-1], FinalEvent)


def test_a_call_to_a_deferred_tool_nothing_loaded_is_served_like_any_offered_tool(shop):
    shop.add_tool("lookup")
    shop.add_tool("hidden")

    _, stopped, _ = shop.run(
        tool_round(tool_call("c1", "hidden", {})), completion("done"), deferred=("hidden",)
    )

    assert stopped is None
    assert [seen[0] for seen in shop.seen] == ["hidden"]


def a_second_turn(shop, first_events, *, deferred):
    """The next turn of the conversation: what the first recorded, the reply and a new message."""
    final = first_events[-1]
    history = a_history(*final.tool_rounds, after=final.text)
    return shop.run(completion("ok"), history=history, deferred=deferred)


def assert_replay_is_byte_equal(first: tuple, second_gateway) -> None:
    last_sent = json.dumps(sent(first[2], len(first[2].requests) - 1)["messages"])
    replayed = sent(second_gateway, 0)["messages"][
        : len(sent(first[2], len(first[2].requests) - 1)["messages"])
    ]
    assert json.dumps(replayed) == last_sent


@dataclass(frozen=True)
class ReplayCase:
    """A turn to replay: the replies it gets, the tools it defers and the options it runs with."""

    replies: tuple
    deferred: tuple[str, ...] = ()
    one_call_per_reply: bool = False


def _lookup_two_calls(shop) -> ReplayCase:
    shop.add_tool("lookup")
    round_ = tool_round(
        tool_call("a", "lookup", {"q": "x"}), tool_call("b", "lookup", {}), content="Looking"
    )
    return ReplayCase((round_, completion("Done.")))


def _lookup_three_calls_one_per_reply(shop) -> ReplayCase:
    shop.add_tool("lookup")
    round_ = tool_round(*(tool_call(f"c{n}", "lookup", {}) for n in range(3)))
    return ReplayCase((round_, completion("Done.")), one_call_per_reply=True)


def _an_error_answer_and_an_image_answer(shop) -> ReplayCase:
    shop.add_tool("failing", answer=answering(ToolOutcome("broken", is_error=True)))
    image = ToolOutcome("seen", is_error=False, image=a_tool_image())
    shop.add_tool("seeing", answer=answering(image))
    round_ = tool_round(tool_call("a", "failing", {}), tool_call("b", "seeing", {}))
    return ReplayCase((round_, completion("Done.")))


def _a_loading_answer_then_the_loaded_tool(shop) -> ReplayCase:
    a_loader_shop(shop)
    replies = (
        tool_round(tool_call("a", "loader", {})),
        tool_round(tool_call("b", "hidden", {"mode": "x"})),
        completion("Done."),
    )
    return ReplayCase(replies, deferred=("hidden",))


REPLAY_CASES = {
    "two-calls": _lookup_two_calls,
    "one-call-per-reply": _lookup_three_calls_one_per_reply,
    "error-and-image": _an_error_answer_and_an_image_answer,
    "loading": _a_loading_answer_then_the_loaded_tool,
}


@pytest.mark.parametrize("arrange", REPLAY_CASES.values(), ids=REPLAY_CASES.keys())
def test_the_recorded_rounds_replay_byte_equal_to_what_the_turn_sent(shop, arrange):
    case = arrange(shop)

    events, stopped, gateway = shop.run(
        *case.replies, deferred=case.deferred, one_call_per_reply=case.one_call_per_reply
    )
    assert stopped is None
    _, second_stopped, second = a_second_turn(shop, events, deferred=case.deferred)

    assert second_stopped is None
    assert_replay_is_byte_equal((events, stopped, gateway), second)


def test_a_round_split_by_one_call_per_reply_records_the_calls_not_run_as_errors(shop):
    case = _lookup_three_calls_one_per_reply(shop)

    events, _, _ = shop.run(*case.replies, one_call_per_reply=True)

    (round_,) = events[-1].tool_rounds
    assert [(call.is_error, call.result) for call in round_.calls] == [
        (False, "noted"),
        (True, NOT_RUN),
        (True, NOT_RUN),
    ]


def test_a_replayed_history_counts_in_the_request_bound(shop):
    shop.add_tool("lookup")
    huge = "x" * (MAX_EVENT_BYTES + 1)
    history = a_history(*(a_tool_round(a_round_call(f"c{n}", result=huge)) for n in range(5)))

    _, stopped, gateway = shop.run(completion("ok"), history=history)

    assert (stopped.kind, stopped.code) == (TurnStopKind.FAILED, FailureCode.LIMIT_EXCEEDED)
    assert gateway.requests == []


def test_replayed_rounds_do_not_count_against_the_call_bound(shop):
    shop.add_tool("lookup")
    history = a_history(a_tool_round(a_round_call("a"), a_round_call("b")))

    _, stopped, _ = shop.run(
        tool_round(tool_call("c1", "lookup", {})),
        completion("ok"),
        history=history,
        max_tool_calls=1,
    )

    assert stopped is None


@pytest.mark.parametrize(
    "mutate",
    [lambda arguments: arguments.pop("q"), lambda arguments: arguments.update(q=object())],
    ids=["removes-an-argument", "stores-a-value-json-cannot-hold"],
)
def test_a_handler_changing_its_arguments_does_not_change_the_round_recorded(shop, mutate):
    async def mutating() -> ToolOutcome:
        mutate(shop.seen[-1][2])
        return ToolOutcome("noted", is_error=False)

    shop.add_tool("lookup", answer=mutating)

    events, stopped, gateway = shop.run(
        tool_round(tool_call("c1", "lookup", {"q": "x"})), completion("done")
    )

    assert stopped is None
    (round_,) = events[-1].tool_rounds
    assert round_.calls[0].arguments == '{"q": "x"}'
    assert sent(gateway, 1)["messages"][-2]["tool_calls"][0]["function"]["arguments"] == (
        '{"q": "x"}'
    )
