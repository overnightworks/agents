"""Builders of the wire dicts an app and an agent exchange about a member."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from logline import current_traceparent, unit_trace
from logline.trace import current_trace_id
from pydantic import BaseModel, ValidationError

from agents.figure_vocabulary import FigureGesture
from agents.member import PlaceWindow


@dataclass(frozen=True)
class WakingTrace:
    traceparent: str
    trace_id: str


def a_waking_trace() -> WakingTrace:
    with unit_trace(None):
        traceparent, trace_id = current_traceparent(), current_trace_id()
    assert traceparent is not None
    assert trace_id is not None
    return WakingTrace(traceparent, trace_id)


def window_from(wire: dict[str, Any]) -> PlaceWindow:
    return PlaceWindow.model_validate_json(json.dumps(wire))


def entry(seq: int, **fields: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": "said", "name": "ada", "text": "hello", "own": False} | fields


def talked_over_wire(**fields: Any) -> dict[str, Any]:
    return {
        "id": "t-1",
        "playing": True,
        "speech": [{"name": "ada", "text": "wait"}],
    } | fields


def talked_window_wire(**fields: Any) -> dict[str, Any]:
    return {
        "generation": "g",
        "starts_at": 1,
        "entries": [entry(1)],
        "talked_over": talked_over_wire(),
    } | fields


def spoken_wire(**fields: Any) -> dict[str, Any]:
    return {"spoken": True} | fields


def written_wire(**fields: Any) -> dict[str, Any]:
    return {"written": True} | fields


def speak_wire(**fields: Any) -> dict[str, Any]:
    return {"place": {"kind": "room", "id": "r-1"}, "text": "Shall we start?"} | fields


def interrupted_wire(**fields: Any) -> dict[str, Any]:
    return {"id": "h-1"} | fields


def stop_wire(**fields: Any) -> dict[str, Any]:
    return {"place": {"kind": "room", "id": "r-1"}} | fields


def continue_wire(**fields: Any) -> dict[str, Any]:
    return {"place": {"kind": "room", "id": "r-1"}, "interrupted": "h-1"} | fields


def turn_state_wire(**fields: object) -> dict[str, object]:
    return {"place": {"kind": "room", "id": "r-1"}} | fields


def gesture_wire(**fields: object) -> dict[str, object]:
    first_gesture = next(iter(FigureGesture)).value
    return {"place": {"kind": "room", "id": "r-1"}, "gesture": first_gesture} | fields


def is_refused(record: type[BaseModel], wire: dict[str, Any]) -> bool:
    """Whether the record refuses the wire as JSON."""
    try:
        record.model_validate_json(json.dumps(wire))
    except ValidationError:
        return True
    return False


def keys_at_any_depth(node: Any) -> set[str]:
    """Every object key found at any depth of a JSON value."""
    if isinstance(node, list):
        return {key for item in node for key in keys_at_any_depth(item)}
    if not isinstance(node, dict):
        return set()
    return set(node) | {key for value in node.values() for key in keys_at_any_depth(value)}
