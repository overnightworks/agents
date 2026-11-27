"""The agent file an app ships, and the definition read from it.

An agent file is Claude Code's subagent format (YAML front matter between two ``---`` lines,
read as YAML 1.2 by ruamel.yaml, then the prompt as Markdown) with one extra key, ``appRoles``,
and two required ones: ``model``, a ``text/<model>`` name for a text agent, and
``maxDurationSeconds``. ``maxToolCalls`` is required exactly when ``tools`` is not empty.
``picture: {size: <width>x<height>}`` makes a picture agent: its ``model`` is an
``image/<model>`` name and it selects no tools. ``voice: {windowBytes: <positive integer>}``
bounds what each decision of the voice loop sends; a picture agent carries none. ``offered_to``
is a display filter for the app's own UI; the boundary is the gateway's group check.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib.resources.abc import Traversable
from typing import Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agents.front_matter import parse_front_matter, split_front_matter
from agents.tools import ToolDeclaration
from agents.turn import (
    AgentName,
    AgentRole,
    AgentRoles,
    GatewayModelName,
    ModelKind,
    ToolRound,
    Turn,
    TurnMessage,
    TurnRole,
    model_kind_of,
)

_AGENT_FILE_SUFFIX: Final = ".md"
_AGENT_ROLES: Final = TypeAdapter(AgentRole)
_AGENT_NAME: Final = TypeAdapter(AgentName)
_PICTURE_SIZE: Final = re.compile(r"[1-9]\d*x[1-9]\d*", re.ASCII)


def _agent_model_kind(*, is_picture: bool) -> ModelKind:
    return ModelKind.IMAGE if is_picture else ModelKind.TEXT


class AgentFileError(ValueError):
    """An agent file is refused; the message names the file and the field."""


def _refuse_non_single_line(text: str) -> str:
    if not text.strip() or "\n" in text or "\r" in text:
        raise ValueError("must be a non-empty single line")
    return text


def _refuse_non_picture_size(size: str) -> str:
    if _PICTURE_SIZE.fullmatch(size) is None:
        raise ValueError("must be <width>x<height>, positive whole numbers")
    return size


PictureSize = Annotated[StrictStr, AfterValidator(_refuse_non_picture_size)]
"""The shape of a picture's size; which sizes exist is the gateway's."""


class _Picture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    size: PictureSize


class _Voice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    window_bytes: StrictInt = Field(alias="windowBytes", gt=0)


class _FrontMatter(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: AgentName
    description: Annotated[StrictStr, AfterValidator(_refuse_non_single_line)]
    tools: list[StrictStr] = Field(default_factory=list)
    roles: AgentRoles = Field(alias="appRoles")
    model: GatewayModelName
    max_duration_seconds: StrictInt = Field(alias="maxDurationSeconds", gt=0)
    max_tool_calls: StrictInt | None = Field(default=None, alias="maxToolCalls", gt=0)
    picture: _Picture | None = None
    voice: _Voice | None = None

    @model_validator(mode="before")
    @classmethod
    def _refuse_keys_without_a_value(cls, data: dict[str, object]) -> dict[str, object]:
        for key, value in data.items():
            if value is None:
                raise ValueError(f"{key}: has no value")
        return data


class TurnRequest(BaseModel):
    """What a backend runs: the agent, its turn, its tools and the model, time and call bounds.

    ``max_tool_calls`` is ``None`` exactly when the agent has no tools. ``one_call_per_reply``
    asks a backend to serve one tool call per model reply; without tools it holds nothing.

    ``deferred_tools`` names tools of ``tools`` sent without their definition loaded, not every
    tool; an answer loads one by naming it in ``loads``, so every recorded round's ``loads`` names
    only deferred tools. Deferral needs a gateway model that honours it.
    """

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    agent: AgentName
    turn: Turn
    roles: AgentRoles
    tools: tuple[ToolDeclaration, ...]
    model: GatewayModelName
    max_duration_seconds: PositiveInt
    max_tool_calls: PositiveInt | None
    picture_size: PictureSize | None = None
    one_call_per_reply: bool = False
    deferred_tools: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _pair_tools_with_their_call_bound(self) -> Self:
        _refuse_unpaired_call_bound(self.tools, self.max_tool_calls)
        return self

    @model_validator(mode="after")
    def _defer_some_tools_and_load_only_deferred_ones(self) -> Self:
        offered = frozenset(tool.name for tool in self.tools)
        if not self.deferred_tools <= offered:
            raise ValueError("deferred_tools names a tool that is not in tools")
        if offered and self.deferred_tools == offered:
            raise ValueError("deferred_tools names every tool; the model needs one loaded")
        loaded = {name for each in self.turn.rounds for call in each.calls for name in call.loads}
        if not loaded <= self.deferred_tools:
            raise ValueError("a tool round loads a tool that is not in deferred_tools")
        return self

    @model_validator(mode="after")
    def _hold_a_picture_to_one_message_and_no_tools(self) -> Self:
        if self.picture_size is None:
            return self
        if self.tools:
            raise ValueError("a picture request selects no tools")
        messages = self.turn.messages
        if (
            len(messages) != 1
            or self.turn.rounds
            or self.turn.conversation[0].role is not TurnRole.USER
        ):
            raise ValueError("a picture request holds exactly one user message")
        return self


class AgentDefinition(BaseModel):
    """One agent as its file states it, with the app's tool declarations it selected."""

    model_config = ConfigDict(frozen=True)

    name: AgentName
    description: str
    roles: AgentRoles
    tools: tuple[ToolDeclaration, ...]
    prompt: str
    model: GatewayModelName
    max_duration_seconds: PositiveInt
    max_tool_calls: PositiveInt | None
    picture_size: PictureSize | None = None
    voice_window_bytes: PositiveInt | None = None

    @model_validator(mode="after")
    def _pair_tools_with_their_call_bound(self) -> Self:
        _refuse_unpaired_call_bound(self.tools, self.max_tool_calls)
        return self

    @model_validator(mode="after")
    def _keep_a_picture_agent_without_a_voice(self) -> Self:
        _refuse_voice_on_a_picture_agent(
            is_picture=self.picture_size is not None, has_voice=self.voice_window_bytes is not None
        )
        return self

    def offered_to(self, role: AgentRole) -> bool:
        """Whether a person with this app role is shown the agent (not a security boundary)."""
        try:
            checked_role = _AGENT_ROLES.validate_python(role, strict=True)
        except ValidationError as error:
            raise ValueError(f"role must be 'admin' or 'user', not {role!r}") from error
        return checked_role in self.roles

    @property
    def model_kind(self) -> ModelKind:
        return _agent_model_kind(is_picture=self.picture_size is not None)

    def request(
        self,
        messages: Sequence[TurnMessage | ToolRound],
        *,
        model: GatewayModelName | None = None,
        appended_system: str | None = None,
        one_call_per_reply: bool = False,
    ) -> TurnRequest:
        """The request that starts a run of this agent over these messages.

        ``model`` overrides the file's model for this run; it must be of the agent's kind
        (``image/…`` for a picture agent), else ``ValueError``. ``appended_system`` follows the
        file's prompt in the system text, after a blank line. ``one_call_per_reply`` is carried
        into the request unchanged.
        """
        system = self.prompt if appended_system is None else f"{self.prompt}\n\n{appended_system}"
        request = TurnRequest(
            agent=self.name,
            turn=Turn(system=system, messages=list(messages)),
            roles=self.roles,
            tools=self.tools,
            model=self.model if model is None else model,
            max_duration_seconds=self.max_duration_seconds,
            max_tool_calls=self.max_tool_calls,
            picture_size=self.picture_size,
            one_call_per_reply=one_call_per_reply,
        )
        if model_kind_of(request.model) is not self.model_kind:
            raise ValueError(f"model: must start with {self.model_kind}/")
        return request


def load_agent(source: Traversable, declarations: list[ToolDeclaration]) -> AgentDefinition:
    """Read and check one agent file against the app's tool declarations.

    Raises ``AgentFileError`` naming the file and the field of the first problem.
    """
    file_name = source.name
    stem = _checked_stem(file_name)
    text = _read_text(source, file_name)
    try:
        front_matter_text, prompt = split_front_matter(text)
    except ValueError as error:
        raise AgentFileError(f"{file_name}: {error}") from None
    front_matter = _parse_front_matter(file_name, front_matter_text)
    if front_matter.name != stem:
        raise AgentFileError(
            f"{file_name}: name {front_matter.name!r} must equal the file name {stem!r}"
        )
    if not prompt:
        raise AgentFileError(f"{file_name}: prompt is empty")
    _refuse_model_of_the_wrong_kind(file_name, front_matter)
    tools = _selected_tools(file_name, front_matter.tools, declarations)
    if front_matter.picture is not None and tools:
        raise AgentFileError(f"{file_name}: tools: a picture agent selects none")
    try:
        _refuse_voice_on_a_picture_agent(
            is_picture=front_matter.picture is not None, has_voice=front_matter.voice is not None
        )
    except ValueError as error:
        raise AgentFileError(f"{file_name}: voice: {error}") from None
    try:
        _refuse_unpaired_call_bound(tools, front_matter.max_tool_calls)
    except ValueError as error:
        raise AgentFileError(f"{file_name}: maxToolCalls: {error}") from None
    return AgentDefinition(
        name=front_matter.name,
        description=front_matter.description,
        roles=front_matter.roles,
        tools=tools,
        prompt=prompt,
        model=front_matter.model,
        max_duration_seconds=front_matter.max_duration_seconds,
        max_tool_calls=front_matter.max_tool_calls,
        picture_size=None if front_matter.picture is None else front_matter.picture.size,
        voice_window_bytes=None if front_matter.voice is None else front_matter.voice.window_bytes,
    )


def _refuse_model_of_the_wrong_kind(file_name: str, front_matter: _FrontMatter) -> None:
    kind = _agent_model_kind(is_picture=front_matter.picture is not None)
    if model_kind_of(front_matter.model) is not kind:
        raise AgentFileError(f"{file_name}: model: must start with {kind}/")


def _refuse_unpaired_call_bound(
    tools: tuple[ToolDeclaration, ...], max_tool_calls: int | None
) -> None:
    """The one rule: a call bound is set exactly when the agent has tools."""
    if tools and max_tool_calls is None:
        raise ValueError("required when tools is not empty")
    if not tools and max_tool_calls is not None:
        raise ValueError("must be absent when tools is empty")


def _refuse_voice_on_a_picture_agent(*, is_picture: bool, has_voice: bool) -> None:
    """The one rule: a picture agent carries no voice section."""
    if is_picture and has_voice:
        raise ValueError("a picture agent carries none")


def _checked_stem(file_name: str) -> str:
    stem = file_name.removesuffix(_AGENT_FILE_SUFFIX)
    if stem == file_name:
        raise AgentFileError(f"{file_name}: an agent file must end in {_AGENT_FILE_SUFFIX}")
    try:
        _AGENT_NAME.validate_python(stem)
    except ValidationError as error:
        raise AgentFileError(
            f"{file_name}: file name stem {stem!r}: {_first_message(error)}"
        ) from None
    return stem


def _read_text(source: Traversable, file_name: str) -> str:
    try:
        return source.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise AgentFileError(f"{file_name}: the file is not UTF-8") from error


def _parse_front_matter(file_name: str, text: str) -> _FrontMatter:
    try:
        parsed = parse_front_matter(text)
    except ValueError as error:
        raise AgentFileError(f"{file_name}: {error}") from None
    try:
        return _FrontMatter.model_validate(parsed)
    except ValidationError as error:
        raise AgentFileError(f"{file_name}: {_first_message(error)}") from None


def _first_message(error: ValidationError) -> str:
    first = error.errors(include_input=False, include_url=False)[0]
    field = ".".join(str(part) for part in first["loc"])
    return f"{field}: {first['msg']}" if field else first["msg"]


def _selected_tools(
    file_name: str, tool_names: list[str], declarations: list[ToolDeclaration]
) -> tuple[ToolDeclaration, ...]:
    by_name: dict[str, ToolDeclaration] = {}
    for declaration in declarations:
        if declaration.name in by_name:
            raise AgentFileError(f"{file_name}: tools: {declaration.name!r} is declared twice")
        by_name[declaration.name] = declaration
    for position, tool_name in enumerate(tool_names):
        if tool_name in tool_names[:position]:
            raise AgentFileError(f"{file_name}: tools: {tool_name!r} is listed twice")
        if tool_name not in by_name:
            raise AgentFileError(f"{file_name}: tools: {tool_name!r} is not a declared tool")
    return tuple(by_name[tool_name] for tool_name in tool_names)
