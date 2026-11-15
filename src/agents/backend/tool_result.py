"""Running one tool call of a turn: its handler's outcome as the result event the model is shown.

A handler is the app's code, so what it raises or answers invalidly is a host defect that fails
the call and not the turn. This module is the one owner of that bound; it writes the call's one
``tool_call.ended`` line with its name and id, never its arguments, content or a handler's message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from logline import StepTimer

from agents.backend import TurnPerson, TurnStopKind, TurnStopped
from agents.tools import HandToolHandler, JsonObject, ToolOutcome
from agents.turn import ToolRefusal, is_within_event_bound
from agents.turn_events import ToolResultEvent

__all__ = ["RequestedCall", "serve"]

_LOGGER: Final = logging.getLogger(__name__)
_HANDLER_FAILED_TEXT: Final = "The tool failed."
_RESULT_TOO_LARGE_TEXT: Final = "The tool result was too large to return."


@dataclass(frozen=True)
class RequestedCall:
    id: str
    name: str
    arguments: JsonObject


async def serve(
    handler: HandToolHandler, person: TurnPerson, call: RequestedCall
) -> ToolResultEvent:
    """Run one handler; an outcome that is no valid result event is a host defect like a raise.

    The one raise that is no defect is ``TurnStopped``: the handler ends the turn, so it passes.
    A refusing stop is the call's ``refused`` line, a failing one its ``failed`` line.
    """
    try:
        async with StepTimer(
            "tool_call.ended", _LOGGER, tool_name=call.name, tool_call_id=call.id
        ) as step:
            step.handled()
            served = await _run(step, handler, person, call)
    except TurnStopped:
        raise
    except Exception:  # a handler is the app's code; its failure is a host defect
        return _failed_event(call, _HANDLER_FAILED_TEXT)
    if isinstance(served, TurnStopped):
        raise served
    return served


async def _run(
    step: StepTimer, handler: HandToolHandler, person: TurnPerson, call: RequestedCall
) -> ToolResultEvent | TurnStopped:
    """The result, or a refusing stop handed back so the line closes before it is raised."""
    try:
        outcome = await handler(person.principal, call.arguments)
    except TurnStopped as stopped:
        if stopped.kind is not TurnStopKind.REFUSED:
            raise
        step.refuse(stopped.code.value)
        return stopped
    return _result_event(step, call, outcome)


def _result_event(step: StepTimer, call: RequestedCall, outcome: ToolOutcome) -> ToolResultEvent:
    event = ToolResultEvent.model_validate(
        {
            "tool_use_id": call.id,
            "content": outcome.content,
            "is_error": outcome.is_error,
            "image": outcome.image,
        },
        strict=True,
    )
    if not is_within_event_bound(event.content, event.image):
        step.refuse(ToolRefusal.RESULT_TOO_LARGE)
        return _failed_event(call, _RESULT_TOO_LARGE_TEXT)
    if event.is_error:
        step.refuse(ToolRefusal.TOOL_ERROR)
    return event


def _failed_event(call: RequestedCall, text: str) -> ToolResultEvent:
    return ToolResultEvent(tool_use_id=call.id, content=text, is_error=True)
