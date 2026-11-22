"""The gateway's request bodies: the chat completion with its messages and tools, and the picture.

Pure: it imports no HTTP library and not ``backend.gateway``, which calls it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final, Literal, NotRequired, Required, TypedDict

from agents.agent_file import TurnRequest
from agents.backend import TurnStopped
from agents.backend.streamed_round import AssembledToolCall
from agents.tools import JsonObject
from agents.turn import MAX_EVENT_BYTES, TOOL_IMAGE_MEDIA_TYPE, written_base64
from agents.turn_events import ToolResultEvent

MAX_REQUEST_BYTES: Final = 4 * MAX_EVENT_BYTES
# The chat format has no error flag on a tool message, so the text carries it to the model.
_ERROR_PREFIX: Final = "Error: "


class _TextPart(TypedDict):
    type: Literal["text"]
    text: str


class _ImageUrl(TypedDict):
    url: str


class _ImagePart(TypedDict):
    type: Literal["image_url"]
    image_url: _ImageUrl


class Message(TypedDict):
    role: Required[str]
    content: NotRequired[str | list[_ImagePart | _TextPart] | None]
    tool_call_id: NotRequired[str]
    tool_calls: NotRequired[list[AssembledToolCall]]


class _FunctionSpec(TypedDict):
    name: str
    description: str
    parameters: JsonObject


class _FunctionTool(TypedDict):
    type: str
    function: _FunctionSpec


class _PictureBody(TypedDict):
    model: str
    prompt: str
    size: str
    user: str


class _StreamOptions(TypedDict):
    include_usage: bool


class _Body(TypedDict):
    model: str
    messages: list[Message]
    user: str
    stream: bool
    stream_options: _StreamOptions
    tools: NotRequired[list[_FunctionTool]]


def opening_messages(request: TurnRequest) -> list[Message]:
    messages: list[Message] = [{"role": "system", "content": request.turn.system}]
    messages += [
        Message(role=message.role.value, content=message.text) for message in request.turn.messages
    ]
    return messages


def result_message(result: ToolResultEvent) -> Message:
    text = _ERROR_PREFIX + result.content if result.is_error else result.content
    if result.image is None:
        return {"role": "tool", "tool_call_id": result.tool_use_id, "content": text}
    # a text part the provider would refuse as blank is left out; the image goes first
    url = f"data:{TOOL_IMAGE_MEDIA_TYPE};base64,{written_base64(result.image.content)}"
    parts: list[_ImagePart | _TextPart] = [{"type": "image_url", "image_url": {"url": url}}]
    if text.strip():
        parts.append({"type": "text", "text": text})
    return {"role": "tool", "tool_call_id": result.tool_use_id, "content": parts}


def request_body(request: TurnRequest, messages: list[Message], user: str) -> str:
    body: _Body = {
        "model": request.model,
        "messages": messages,
        "user": user,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        # ``effect`` is for the client, so the projection names its fields.
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
    return json.dumps(body)


def picture_body(request: TurnRequest, size: str, user: str) -> str:
    prompt = f"{request.turn.system}\n\n{request.turn.messages[0].text}"
    body: _PictureBody = {
        "model": request.model,
        "prompt": prompt,
        "size": size,
        "user": user,
    }
    return json.dumps(body)


def bounded(body: str, exceeded: Callable[[], TurnStopped]) -> str:
    """A request over the one bound is not sent."""
    if len(body.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise exceeded()
    return body
