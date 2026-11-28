"""What the member session reads out of an app's answers: records, tools, pictures and refusals.

A refusal the app answers with is a value; an answer that is none of the records this release
knows is an ``UnusableAnswer`` that carries none of the app's content.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, override

from mcp_types import (
    CallToolResult,
    ImageContent,
    ReadResourceResult,
    TextContent,
    TextResourceContents,
    Tool,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

from agents.member import (
    AcceptInvitationRefusal,
    AcceptInvitationRefused,
    DeclineInvitationRefusal,
    DeclineInvitationRefused,
    MemberResourceRecord,
    PlaceRef,
    SpeakAnswer,
    SpeakRefusal,
    SpeakRefused,
    member_resource_from_json,
)
from agents.member_outcomes import INVALID_ANSWER_CODE, UnrecognisedRefusal, UnusableAnswer
from agents.tools import JsonObject, ToolDeclaration, ToolEffect, ToolImage
from agents.turn import TOOL_IMAGE_MEDIA_TYPE

_SPEAK_ANSWER: Final = TypeAdapter(SpeakAnswer)


def record_of[Record: MemberResourceRecord](
    record: type[Record], result: ReadResourceResult
) -> Record:
    text = "".join(
        block.text for block in result.contents if isinstance(block, TextResourceContents)
    )
    try:
        return member_resource_from_json(record, text)
    except ValidationError:
        raise UnusableAnswer(INVALID_ANSWER_CODE) from None


def declaration_of(tool: Tool) -> ToolDeclaration:
    read_only = tool.annotations is not None and bool(tool.annotations.read_only_hint)
    return ToolDeclaration(
        name=tool.name,
        description=tool.description or "",
        parameters=tool.input_schema,
        effect=ToolEffect.READ_ONLY if read_only else ToolEffect.SIDE_EFFECTING,
    )


def text_of(result: CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, TextContent))


class _UnshownToolImage(ToolImage):
    """A tool image whose representation names no byte of the picture."""

    @override
    def __repr_args__(self) -> Iterable[tuple[str | None, object]]:
        return ()


def picture_of(result: CallToolResult) -> ToolImage | None:
    """The one picture of the result, as the wire carries it; none when the result holds no image
    block.

    An image block is the wire's picture, so one that breaks the wire, a second one included, is an
    unusable answer. Audio and resource blocks are not read.
    """
    blocks = [block for block in result.content if isinstance(block, ImageContent)]
    if not blocks:
        return None
    if len(blocks) > 1 or blocks[0].mime_type != TOOL_IMAGE_MEDIA_TYPE:
        raise UnusableAnswer(INVALID_ANSWER_CODE)
    try:
        return _UnshownToolImage.model_validate({"content": blocks[0].data})
    except ValidationError:
        raise UnusableAnswer(INVALID_ANSWER_CODE) from None


def accept_refusal_of(text: str) -> AcceptInvitationRefusal | UnrecognisedRefusal:
    try:
        return AcceptInvitationRefused.model_validate_json(text).refused
    except ValidationError:
        return UnrecognisedRefusal.UNRECOGNISED


def decline_refusal_of(text: str) -> DeclineInvitationRefusal | UnrecognisedRefusal:
    try:
        return DeclineInvitationRefused.model_validate_json(text).refused
    except ValidationError:
        return UnrecognisedRefusal.UNRECOGNISED


def speak_answer_of(result: CallToolResult) -> SpeakAnswer | SpeakRefusal:
    text = text_of(result)
    try:
        if result.is_error:
            return SpeakRefused.model_validate_json(text).refused
        return _SPEAK_ANSWER.validate_json(text)
    except ValidationError:
        raise UnusableAnswer(INVALID_ANSWER_CODE) from None


def naming_hold(place: PlaceRef, interrupted: str, *, finish: bool = False) -> JsonObject:
    """The wire's arguments of ``continue_speaking`` and ``stop_speaking``, built unvalidated as
    ``speak``'s are; ``finish`` is the one ``continue_speaking`` takes, left out while false."""
    arguments: JsonObject = {"place": place.model_dump(mode="json"), "interrupted": interrupted}
    if finish:
        arguments["finish"] = True
    return arguments


def refusal_in[Refused: BaseModel](refused: type[Refused], result: CallToolResult) -> Refused:
    try:
        return refused.model_validate_json(text_of(result))
    except ValidationError:
        raise UnusableAnswer(INVALID_ANSWER_CODE) from None
