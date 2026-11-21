"""Where a place is: its reference and the member resource URIs that name it.

This module is base, so a runner and an app name a place and its resources without the
``mcp-server`` extra. ``agents.member`` re-exports every name.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote, unquote

from pydantic import ValidationError

from agents.turn import NonEmptyUnicodeText, StrictModel

__all__ = [
    "STANDING_URI",
    "PlaceRef",
    "place_ref_from_view_uri",
    "place_ref_from_window_uri",
    "place_view_uri",
    "place_window_uri",
]

STANDING_URI: Final = "member://standing"
"""The resource that answers with the member's ``MemberStanding``."""
_PLACE_WINDOW_URI_PREFIX: Final = f"{STANDING_URI}/places/"
_VIEW_SEGMENT: Final = "/view"


class PlaceRef(StrictModel):
    """A place the member is in, opaque to everyone but the app: its kind and its id."""

    kind: NonEmptyUnicodeText
    id: NonEmptyUnicodeText


def place_window_uri(place: PlaceRef) -> str:
    """The resource of one place's window; each segment is escaped, so ``/`` stays opaque."""
    return _PLACE_WINDOW_URI_PREFIX + f"{quote(place.kind, safe='')}/{quote(place.id, safe='')}"


def place_ref_from_window_uri(uri: object) -> PlaceRef | None:
    """The place a window URI names, or ``None`` for anything but its canonical spelling."""
    if not isinstance(uri, str) or not uri.startswith(_PLACE_WINDOW_URI_PREFIX):
        return None
    segments = uri.removeprefix(_PLACE_WINDOW_URI_PREFIX).split("/")
    if len(segments) != 2 or not all(segments):
        return None
    try:
        kind, place_id = (unquote(segment, errors="strict") for segment in segments)
        place = PlaceRef(kind=kind, id=place_id)
    except UnicodeDecodeError, ValidationError:
        return None
    return place if place_window_uri(place) == uri else None


def place_view_uri(place: PlaceRef) -> str:
    """The resource of one place's view: the place's window URI and the segment ``view``."""
    return place_window_uri(place) + _VIEW_SEGMENT


def place_ref_from_view_uri(uri: object) -> PlaceRef | None:
    """The place a view URI names, or ``None`` for anything but its canonical spelling."""
    if not isinstance(uri, str) or not uri.endswith(_VIEW_SEGMENT):
        return None
    return place_ref_from_window_uri(uri.removesuffix(_VIEW_SEGMENT))
