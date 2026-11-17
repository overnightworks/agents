"""How an agent presents itself in an accept: the file reference of its 3D figure and its looks,
and the names of the voices it would speak with.

A host types its own look or voice list with ``Looks`` and ``Voices`` to refuse a malformed one
before an accept, by the same rules ``AcceptInvitationArguments`` applies. Everything here is
re-exported by ``agents.member``, which a runner and a host import it from.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, StringConstraints, field_validator

from agents.figure_vocabulary import FigureGesture
from agents.turn import StrictModel

__all__ = [
    "FigureLook",
    "FigureRef",
    "HttpsFileRef",
    "Look",
    "LookName",
    "Looks",
    "VideoLook",
    "VoiceName",
    "Voices",
]

_FORBIDDEN_HOST_CHARACTERS: Final = frozenset('%"<>^`{|}')


class HttpsFileRef(StrictModel):
    """Where a file is and the digest its bytes must have: ``url`` is an absolute ``https``
    address with a host and no user part, query or fragment, and ``sha256`` is 64 lowercase
    hexadecimal characters. The record checks the form only; whether to fetch the file is the
    app's decision."""

    url: str
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("url")
    @classmethod
    def _url_is_a_plain_https_address(cls, url: str) -> str:
        message = "a url is an absolute https address without user part, query or fragment"
        if (
            not url.isprintable()
            or any(character.isspace() or character == "\\" for character in url)
            or "?" in url
            or "#" in url
        ):
            raise ValueError(message)
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - reading the port is what validates it
        except ValueError as error:
            raise ValueError(message) from error
        if (
            parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or any(character in _FORBIDDEN_HOST_CHARACTERS for character in parts.hostname)
        ):
            raise ValueError(message)
        return url


def _is_empty(value: dict[FigureGesture, HttpsFileRef]) -> bool:
    return not value


class FigureRef(HttpsFileRef):
    """Where an agent's 3D figure is (see ``HttpsFileRef``) and, optionally, its own clips: for a
    gesture of figure's base set, the file that replaces figure's neutral clip on this figure
    only. A gesture has at most one clip, and ``gestures`` is left out of the JSON when empty."""

    gestures: dict[Annotated[FigureGesture, Field(strict=False)], HttpsFileRef] = Field(
        default_factory=dict[FigureGesture, HttpsFileRef], exclude_if=_is_empty
    )


_NAME_PATTERN: Final = r"^[A-Za-z0-9._-]{1,64}$"

VoiceName = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]
"""The name of a voice an agent would speak with: 1 to 64 ASCII letters of either case, digits,
``.``, ``_`` or ``-``. The name is passed through unchanged; the app's speech service decides
whether it offers that voice."""

LookName = Annotated[str, StringConstraints(pattern=_NAME_PATTERN)]
"""The name of a video look: 1 to 64 ASCII letters of either case, digits, ``.``, ``_`` or ``-``,
the form of a ``VoiceName``. The name is passed through unchanged; the app's video service decides
whether it offers that look."""


def _name_no_voice_twice(voices: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(voices)) != len(voices):
        raise ValueError("a voice is named at most once")
    return voices


Voices = Annotated[
    tuple[VoiceName, ...],
    Field(min_length=1, max_length=8),
    AfterValidator(_name_no_voice_twice),
]
"""The voices an agent would speak with, first choice first: 1 to 8 ``VoiceName`` entries, none
twice. A host types its own voice list with it to refuse a malformed one before an accept, by the
same rule ``AcceptInvitationArguments.voices`` applies."""


class FigureLook(FigureRef):
    """A look of the agent that is its 3D figure: the fields of ``FigureRef`` and ``kind``, the
    ``figure`` tag that tells the look from a ``VideoLook``. A figure look plays gestures."""

    kind: Literal["figure"]


class VideoLook(StrictModel):
    """A look of the agent that a video service renders, named by ``name``: the app's video
    service decides which look the name stands for."""

    kind: Literal["video"]
    name: LookName


Look = Annotated[FigureLook | VideoLook, Field(discriminator="kind")]
"""One look of an agent: a ``FigureLook`` or a ``VideoLook``, told by ``kind``."""


def _name_at_most_one_figure_and_no_video_twice(looks: tuple[Look, ...]) -> tuple[Look, ...]:
    if sum(isinstance(look, FigureLook) for look in looks) > 1:
        raise ValueError("a figure look is named at most once")
    video_names = [look.name for look in looks if isinstance(look, VideoLook)]
    if len(set(video_names)) != len(video_names):
        raise ValueError("a video look is named at most once")
    return looks


Looks = Annotated[
    tuple[Look, ...],
    Field(min_length=1),
    AfterValidator(_name_at_most_one_figure_and_no_video_twice),
]
"""The looks an agent has, first choice first: at least one ``Look``, at most one of kind
``figure``, no video name twice. A host types its own look list with it to refuse a malformed one
before an accept, by the same rule ``AcceptInvitationArguments.looks`` applies."""
