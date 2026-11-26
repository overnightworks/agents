"""What an app and an agent say to each other about a member, written once.

An app writes these records and an agent's runner reads them: where the member stands in every
place (``Standing``), the window of lines one place shows (``PlaceWindow``), the state of one
place (``PlaceView``), the arguments of accepting and declining an invitation, of speaking,
stopping and continuing in a place and of setting the framing, what accepting, declining and
setting the framing answer when they refuse, what speaking and stopping answer (a sentence voiced,
or written as a line when the voice failed), and what the window says when people talk over the
member (``TalkedOver``), when the member is held by it (``Interrupted``), when it went on by itself
(``WentOn``) and whether the member's voice at the place has failed (``VoiceState``).
``MemberFacts`` states who a member is, owner and name, as a lease keeps them. A runner reads a standing, window or view with
``member_resource_from_json``, which ignores a key its release does not know. The
URIs name the resources that serve them; a URI is no credential, so a guessed one yields nothing.
This module is base, so a runner parses all of it without the ``mcp-server`` extra.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Self, get_args

from pydantic import (
    AnyHttpUrl,
    AwareDatetime,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from agents.figure_vocabulary import FigureFraming, FigureGesture
from agents.member_place import (
    STANDING_URI,
    PlaceRef,
    place_ref_from_view_uri,
    place_ref_from_window_uri,
    place_view_uri,
    place_window_uri,
)
from agents.member_presentation import (
    FigureLook,
    FigureRef,
    HttpsFileRef,
    Look,
    LookName,
    Looks,
    VideoLook,
    VoiceName,
    Voices,
)
from agents.member_speaking import (
    ContinuedSpeaking,
    ContinueSpeakingArguments,
    ContinueSpeakingRefusal,
    ContinueSpeakingRefused,
    DroppedSpeaking,
    HoldPlace,
    HoldPoint,
    Interrupted,
    MarkerTrue,
    PartialSpeech,
    SpeakAnswer,
    SpeakArguments,
    SpeakRefusal,
    SpeakRefused,
    Spoken,
    SpokenLanguage,
    StoppedSpeaking,
    StopSpeakingAnswer,
    StopSpeakingArguments,
    StopSpeakingRefusal,
    StopSpeakingRefused,
    TalkedOver,
    VoiceState,
    VoiceStyle,
    WentOn,
    Written,
    has_words,
    hold_place,
)
from agents.tools import ToolDeclaration, ToolEffect, tool_parameters
from agents.turn import JsonObject, NonEmptyUnicodeText, StopOutcome, StrictModel, UnicodeText

__all__ = [
    "ACCEPT_INVITATION_TOOL",
    "CONTINUE_SPEAKING_TOOL",
    "DECLINE_INVITATION_TOOL",
    "GESTURE_TOOL",
    "INVITATION_SECONDS",
    "LEASE_SECONDS",
    "LISTEN_ALLOWANCE_SECONDS",
    "LISTEN_BOUND_SECONDS",
    "MEMBER_RESOURCE_RECORDS",
    "MEMBER_TOOL_ARGUMENTS",
    "MEMBER_TOOL_RESULTS",
    "SET_FRAMING_TOOL",
    "SET_TURN_STATE_TOOL",
    "SPEAK_TOOL",
    "STANDING_URI",
    "STOP_SPEAKING_TOOL",
    "AcceptInvitationArguments",
    "AcceptInvitationRefusal",
    "AcceptInvitationRefused",
    "ContinueSpeakingArguments",
    "ContinueSpeakingRefusal",
    "ContinueSpeakingRefused",
    "ContinuedSpeaking",
    "DeclineInvitationArguments",
    "DeclineInvitationReason",
    "DeclineInvitationRefusal",
    "DeclineInvitationRefused",
    "DroppedSpeaking",
    "FigureLook",
    "FigureRef",
    "GestureArguments",
    "GestureRefusal",
    "GestureRefused",
    "HoldPlace",
    "HoldPoint",
    "HttpsFileRef",
    "Interrupted",
    "Invitation",
    "Look",
    "LookName",
    "Looks",
    "MemberFacts",
    "MemberResourceRecord",
    "MemberStanding",
    "OptionalTraceparent",
    "ParentPlace",
    "PartialSpeech",
    "PlaceEntry",
    "PlaceRef",
    "PlaceView",
    "PlaceWindow",
    "Relayed",
    "SetFramingArguments",
    "SetFramingRefusal",
    "SetFramingRefused",
    "SetTurnStateArguments",
    "SetTurnStateRefusal",
    "SetTurnStateRefused",
    "SpeakAnswer",
    "SpeakArguments",
    "SpeakRefusal",
    "SpeakRefused",
    "Spoken",
    "SpokenLanguage",
    "Standing",
    "StandingState",
    "StopSpeakingAnswer",
    "StopSpeakingArguments",
    "StopSpeakingRefusal",
    "StopSpeakingRefused",
    "StoppedSpeaking",
    "TalkedOver",
    "VideoLook",
    "VoiceName",
    "VoiceState",
    "VoiceStyle",
    "Voices",
    "WentOn",
    "Written",
    "framing_tool",
    "gesture_tool",
    "has_words",
    "hold_place",
    "member_arguments",
    "member_resource_from_json",
    "place_ref_from_view_uri",
    "place_ref_from_window_uri",
    "place_view_uri",
    "place_window_uri",
]

LEASE_SECONDS: Final = 120
"""How long a member's lease lasts; each admitted call renews it."""


class MemberFacts(StrictModel):
    """Who a member is, as its verified token states it at its latest admitted call: its owner's
    subject and its own name. A lease keeps them for ``LEASE_SECONDS`` from the renewal that
    carried them."""

    owner: NonEmptyUnicodeText
    name: UnicodeText


INVITATION_SECONDS: Final = 120
"""How long an invitation stands before it expires; the app opens it with this length."""
LISTEN_BOUND_SECONDS: Final = 50
"""The longest a listen lasts before it ends with the completion result."""
LISTEN_ALLOWANCE_SECONDS: Final = 5
"""How long an ended listen still counts as one: a runner listens again at once, which takes the
connection and one call of the port (a few milliseconds plus the port's latency), so the answer
never turns no between two listens; far below ``LEASE_SECONDS``."""
ACCEPT_INVITATION_TOOL: Final = "accept_invitation"
"""The name of the tool that accepts an open invitation."""
DECLINE_INVITATION_TOOL: Final = "decline_invitation"
"""The name of the tool that declines an open invitation."""
SPEAK_TOOL: Final = "speak"
"""The name of the tool that says one sentence aloud in a place."""
STOP_SPEAKING_TOOL: Final = "stop_speaking"
"""The name of the tool that cuts the member's current sentence and its queue, or drops a held
one."""
CONTINUE_SPEAKING_TOOL: Final = "continue_speaking"
"""The name of the tool that lets a held sentence go on."""
GESTURE_TOOL: Final = "gesture"
"""The name of the tool that makes a gesture with the member's figure at a place."""
SET_FRAMING_TOOL: Final = "set_framing"
"""The name of the tool that sets how much of the member's figure shows at a place."""
SET_TURN_STATE_TOOL: Final = "set_turn_state"
"""The name of the tool that states whether the member's turns at a place answer."""


class StandingState(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    PAUSED = "paused"


class Invitation(StrictModel):
    """An open invitation; ``epoch`` is the place's removal epoch, so an accept after a removal
    is refused by the app."""

    id: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    expires_at: AwareDatetime


class ParentPlace(StrictModel):
    """The place a standing's place lies inside: ``app`` is the parent app's MCP resource URL and
    ``place`` the place there. A runner compares both with a place it holds, ``app`` as a parsed
    URL, so equal addresses in another spelling match; the record grants nothing."""

    app: AnyHttpUrl
    place: PlaceRef


def _is_false(value: bool) -> bool:
    return not value


def _is_none(value: object) -> bool:
    return value is None


class Standing(StrictModel):
    """One place, the member's state there and, exactly when invited, the invitation."""

    place: PlaceRef
    state: Annotated[StandingState, Field(strict=False)]
    invitation: Invitation | None = None
    serves_view: bool = Field(default=False, exclude_if=_is_false)
    """Whether the app serves this place's ``PlaceView`` beside its window; both URIs are then
    listed among the member's current resources."""
    parent: ParentPlace | None = Field(default=None, exclude_if=_is_none)
    """The place this place lies inside, if another app's place holds it; the app sets it from
    the invitation on and keeps it while the member holds the place."""

    @model_validator(mode="after")
    def _invitation_exactly_when_invited(self) -> Self:
        if (self.state is StandingState.INVITED) != (self.invitation is not None):
            raise ValueError("an invitation is carried exactly when the state is invited")
        return self


class MemberStanding(StrictModel):
    """Every place the member is in at the moment it was read; a place left is absent."""

    standings: tuple[Standing, ...]


class AcceptInvitationArguments(StrictModel):
    """The arguments of ``accept_invitation``: the place and the invitation's id and epoch, and
    optionally the agent's ``looks``, first choice first, its ``figure`` and its ``voices``, the
    names of the voices it would speak with, first choice first; an absent one means the app's own
    default. The app shows the first look it can; ``figure`` is read only while ``looks`` is
    absent. The app passes the ``voices`` unchanged and in order to its speech service, which
    picks the voice."""

    place: PlaceRef
    invitation: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    looks: Looks | None = Field(default=None, exclude_if=_is_none)
    figure: FigureRef | None = Field(default=None, exclude_if=_is_none)
    voices: Voices | None = Field(default=None, exclude_if=_is_none)


class AcceptInvitationRefusal(StrEnum):
    """Why the app does not accept an invitation: ``stale_invitation`` when the invitation is no
    longer standing, has expired, or its id or epoch no longer matches, or the member holds no
    lease; ``two_seats`` when the person who brought the member does not hold exactly
    one seat in the place, the inviting one; ``sponsor_away`` when that person's seat lost its
    connection and waits to resume; ``busy`` when the place has no free seat."""

    STALE_INVITATION = "stale_invitation"
    TWO_SEATS = "two_seats"
    SPONSOR_AWAY = "sponsor_away"
    BUSY = "busy"


class AcceptInvitationRefused(StrictModel):
    """What ``accept_invitation`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[AcceptInvitationRefusal, Field(strict=False)]


class DeclineInvitationReason(StrEnum):
    """Why the runner declines an invitation: ``busy`` when the runner is at its own limit of how
    many places it is in at once; not ``AcceptInvitationRefusal.BUSY``, which is the app's full
    place."""

    BUSY = "busy"


class DeclineInvitationArguments(StrictModel):
    """The arguments of ``decline_invitation``: the place, the invitation's id and epoch, and the
    reason; a decline needs no lease, as it seats nothing."""

    place: PlaceRef
    invitation: NonEmptyUnicodeText
    epoch: int = Field(ge=0)
    reason: Annotated[DeclineInvitationReason, Field(strict=False)]


class DeclineInvitationRefusal(StrEnum):
    """Why the app does not take a decline: ``stale_invitation`` when the invitation is not that
    member's, no longer stands, has expired, or its id or epoch no longer matches."""

    STALE_INVITATION = "stale_invitation"


class DeclineInvitationRefused(StrictModel):
    """What ``decline_invitation`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[DeclineInvitationRefusal, Field(strict=False)]


class SetTurnStateArguments(StrictModel):
    """The arguments of ``set_turn_state``: the place and why the member's turns there stop; an
    absent ``stopped`` states that they answer. The value is a closed outcome, never free text.
    ``thinking`` states instead that a turn is running there; it does not fit with ``stopped``."""

    place: PlaceRef
    stopped: Annotated[StopOutcome, Field(strict=False)] | None = Field(
        default=None, exclude_if=_is_none
    )
    thinking: bool = Field(default=False, exclude_if=_is_false)

    @field_validator("thinking")
    @classmethod
    def _a_running_turn_has_no_stop(cls, thinking: bool, info: ValidationInfo) -> bool:
        """Checked here so a refusal names ``thinking``; a model-level error has no path."""
        if thinking and info.data.get("stopped") is not None:
            raise ValueError("a running turn has no stop outcome")
        return thinking


class SetTurnStateRefusal(StrEnum):
    """Why the app does not take a statement: ``not_held`` when the caller holds no seat, active or
    paused, at the place."""

    NOT_HELD = "not_held"


class SetTurnStateRefused(StrictModel):
    """What ``set_turn_state`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[SetTurnStateRefusal, Field(strict=False)]


# The docstring is the schema's description, so it is written for the model.
class GestureArguments(StrictModel):
    """The place and the gesture your figure makes there."""

    place: PlaceRef
    gesture: Annotated[FigureGesture, Field(strict=False)]


class SetFramingArguments(StrictModel):
    """The place and the framing your figure takes there: how much of you the people see, from your
    face to your whole body. The framing stays until you set another."""

    place: PlaceRef
    framing: Annotated[FigureFraming, Field(strict=False)]


MEMBER_TOOL_ARGUMENTS: Final[Mapping[str, type[StrictModel]]] = MappingProxyType(
    {
        ACCEPT_INVITATION_TOOL: AcceptInvitationArguments,
        DECLINE_INVITATION_TOOL: DeclineInvitationArguments,
        SPEAK_TOOL: SpeakArguments,
        STOP_SPEAKING_TOOL: StopSpeakingArguments,
        CONTINUE_SPEAKING_TOOL: ContinueSpeakingArguments,
        GESTURE_TOOL: GestureArguments,
        SET_FRAMING_TOOL: SetFramingArguments,
        SET_TURN_STATE_TOOL: SetTurnStateArguments,
    }
)
"""The arguments record of each member tool by the tool's name. The person front checks a call
against it before the handler runs."""


def member_arguments[ArgumentsT: StrictModel](
    model: type[ArgumentsT], arguments: JsonObject
) -> ArgumentsT:
    """The arguments as the model, parsed as JSON as an agent sends them (a list is a ``Voices``
    tuple); a ``ValidationError`` when they do not fit it."""
    return model.model_validate_json(json.dumps(arguments))


class Relayed(StrictModel):
    """What ``gesture`` answers, as JSON and without ``is_error``, when the app has handed the
    gesture to the people at the place; the field is exactly the JSON ``true``. It does not say
    that a page played it."""

    relayed: MarkerTrue


class GestureRefusal(StrEnum):
    """Why the app does not relay a gesture: ``not_active`` when the member left, was removed or
    is paused."""

    NOT_ACTIVE = "not_active"


class GestureRefused(StrictModel):
    """What ``gesture`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[GestureRefusal, Field(strict=False)]


class SetFramingRefusal(StrEnum):
    """Why the app does not take a framing: ``not_active`` when the member left, was removed or is
    paused."""

    NOT_ACTIVE = "not_active"


class SetFramingRefused(StrictModel):
    """What ``set_framing`` answers on a refusal, as JSON in an ``is_error`` result."""

    refused: Annotated[SetFramingRefusal, Field(strict=False)]


MEMBER_TOOL_RESULTS: Final[Mapping[str, tuple[type[StrictModel], ...]]] = MappingProxyType(
    {
        ACCEPT_INVITATION_TOOL: (AcceptInvitationRefused,),
        DECLINE_INVITATION_TOOL: (DeclineInvitationRefused,),
        SPEAK_TOOL: (Spoken, Written, SpeakRefused),
        STOP_SPEAKING_TOOL: (StoppedSpeaking, DroppedSpeaking, StopSpeakingRefused),
        CONTINUE_SPEAKING_TOOL: (ContinuedSpeaking, ContinueSpeakingRefused),
        GESTURE_TOOL: (Relayed, GestureRefused),
        SET_FRAMING_TOOL: (SetFramingRefused,),
        SET_TURN_STATE_TOOL: (SetTurnStateRefused,),
    }
)
"""The answer records each member tool may return, as JSON, by the tool's name; the keys are
those of ``MEMBER_TOOL_ARGUMENTS``. A success the app does not pin to a record (an accepted
invitation, a decline, a framing, a turn state) lists only its refusal."""


def gesture_tool() -> ToolDeclaration:
    """The declaration of ``GESTURE_TOOL`` as an agent sees it. An app offers it only while the
    seat's shown look plays gestures; its schema's enum is the gestures the agent can make."""
    return ToolDeclaration(
        name=GESTURE_TOOL,
        description="Make a gesture with your figure at a place, as a person waves.",
        parameters=tool_parameters(GestureArguments),
        effect=ToolEffect.SIDE_EFFECTING,
    )


def framing_tool() -> ToolDeclaration:
    """The declaration of ``SET_FRAMING_TOOL`` as an agent sees it. An app offers it only while the
    seat's shown look plays framings; its schema's enum is the framings the agent can take."""
    return ToolDeclaration(
        name=SET_FRAMING_TOOL,
        description="Set how much of your figure shows at a place, from your face to your body.",
        parameters=tool_parameters(SetFramingArguments),
        effect=ToolEffect.SIDE_EFFECTING,
    )


OptionalTraceparent = Annotated[
    Annotated[str, StringConstraints(max_length=256)] | None,
    Field(default=None, exclude_if=_is_none),
]
"""A W3C ``traceparent`` as an app stores it, left out of the JSON when absent. Only its length is
bound here: logline's ``unit_trace`` is the one reader and starts a new trace for a value it
cannot parse."""


class PlaceEntry(StrictModel):
    """One line of a place; ``kind`` is the app's own text, ``own`` marks the member's line and
    ``spoken`` a line said aloud and heard, one finished utterance, rather than written. A line
    enters once its sentence has ended and is never changed; ``cut`` marks a spoken sentence cut
    before its end, whose ``text`` is the part heard up to the cut, or the whole sentence when the
    app cannot tell how far it played. ``act`` marks a written line that says what someone did
    rather than what someone said: it is told and never calls the member to act. ``continues``
    marks a spoken line whose speaker's next sentence at the place already waits to play as the
    line enters; the app copies that fact and decides nothing on it. ``language`` is the ISO 639-1
    code a spoken line was heard in; absent, the app names none. ``traceparent`` is the trace the
    line entered in, which a runner continues in the turn the line wakes."""

    seq: int = Field(ge=1)
    kind: NonEmptyUnicodeText
    name: NonEmptyUnicodeText
    text: NonEmptyUnicodeText
    own: bool
    spoken: bool = False
    cut: bool = Field(default=False, exclude_if=_is_false)
    act: bool = Field(default=False, exclude_if=_is_false)
    continues: bool = Field(default=False, exclude_if=_is_false)
    language: SpokenLanguage | None = Field(default=None, exclude_if=_is_none)
    traceparent: OptionalTraceparent

    @model_validator(mode="after")
    def _only_a_spoken_line_is_cut(self) -> Self:
        if self.cut and not self.spoken:
            raise ValueError("only a spoken line is cut")
        return self

    @model_validator(mode="after")
    def _a_spoken_line_is_no_act(self) -> Self:
        if self.act and self.spoken:
            raise ValueError("a spoken line is no act")
        return self

    @model_validator(mode="after")
    def _only_a_spoken_line_continues(self) -> Self:
        if self.continues and not self.spoken:
            raise ValueError("only a spoken line continues")
        return self

    @model_validator(mode="after")
    def _only_a_spoken_line_has_a_language(self) -> Self:
        if self.language is not None and not self.spoken:
            raise ValueError("only a spoken line has a language")
        return self

    @model_validator(mode="after")
    def _a_cut_line_does_not_continue(self) -> Self:
        if self.continues and self.cut:
            raise ValueError("a cut line does not continue")
        return self


class PlaceWindow(StrictModel):
    """The lines of one place from ``starts_at`` on, in ascending ``seq``; ``seq`` may skip.

    ``generation`` names the run of ``seq`` numbers the window belongs to. An empty window is
    valid, and its ``starts_at`` is the ``seq`` the next line will get. ``talked_over`` is set
    only while people talk over the member; a person's partial and finished line are never both
    in one read. ``interrupted`` is set exactly while the member is held, and leaves in the same
    change that ends the hold; a new ``generation`` carries none. ``went_on`` names the latest hold
    of this ``generation`` that ended by itself; it arises in the change that ends that hold and
    stays until a later one replaces it. ``voice_state`` states a failure of the member's voice at
    the place, for that member there; absent, the app states nothing.
    """

    generation: NonEmptyUnicodeText
    starts_at: int = Field(ge=1)
    entries: tuple[PlaceEntry, ...]
    talked_over: TalkedOver | None = Field(default=None, exclude_if=_is_none)
    interrupted: Interrupted | None = Field(default=None, exclude_if=_is_none)
    went_on: WentOn | None = Field(default=None, exclude_if=_is_none)
    voice_state: Annotated[VoiceState, Field(strict=False)] | None = Field(
        default=None, exclude_if=_is_none
    )

    @model_validator(mode="after")
    def _entries_ascend_from_the_start(self) -> Self:
        seqs = [line.seq for line in self.entries]
        if any(later <= earlier for earlier, later in zip(seqs, seqs[1:], strict=False)):
            raise ValueError("entries must have strictly ascending seq")
        if seqs and seqs[0] != self.starts_at:
            raise ValueError("a non-empty window starts at its first entry's seq")
        return self


class PlaceView(StrictModel):
    """The state of one place for the member, as the app serves it.

    ``state`` is the app's document, carried untouched and never read here. ``revision`` changes
    whenever the place moves on for this member, that is whenever what the member may do or must
    act on changes; parts of ``state`` that say only who is present may change without it.
    ``awaits_you`` says the place waits for this member to act; the app decides it from the state
    at that revision, so one revision has one ``awaits_you``. ``traceparent`` is the trace the
    revision moved in, which a runner continues in the turn it wakes.
    """

    revision: int = Field(ge=0)
    awaits_you: bool
    state: JsonObject
    traceparent: OptionalTraceparent


type MemberResourceRecord = MemberStanding | PlaceWindow | PlaceView
"""The records a member resource carries as JSON, the ones ``MemberResources.read`` returns."""

MEMBER_RESOURCE_RECORDS: Final[tuple[type[StrictModel], ...]] = get_args(
    MemberResourceRecord.__value__
)
"""Each of ``MemberResourceRecord``; a record they nest travels inside their schema."""


def member_resource_from_json[RecordT: MemberResourceRecord](
    record: type[RecordT], text: str | bytes
) -> RecordT:
    """``record`` read from ``text``, a key of any depth this release does not know dropped, never
    carried; every field, enum value and rule across fields it knows is checked as the default parse
    does, and a view's ``state`` keeps every key."""
    return record.model_validate_json(text, extra="ignore")
