"""What an agent says to an app to accept or decline an invitation: the argument records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agents.member import (
    AcceptInvitationArguments,
    DeclineInvitationArguments,
    DeclineInvitationReason,
    Invitation,
    PlaceRef,
    Standing,
    StandingState,
)

PLACE = PlaceRef(kind="room", id="r-1")
INVITATION = Invitation(id="i-1", epoch=3, expires_at=datetime(2026, 11, 8, 12, tzinfo=UTC))


def test_accept_invitation_arguments_take_the_place_straight_from_the_standing() -> None:
    standing = Standing(place=PLACE, state=StandingState.INVITED, invitation=INVITATION)
    assert standing.invitation is not None

    arguments = AcceptInvitationArguments(
        place=standing.place, invitation=standing.invitation.id, epoch=standing.invitation.epoch
    )

    assert json.loads(arguments.model_dump_json()) == {
        "place": {"kind": "room", "id": "r-1"},
        "invitation": "i-1",
        "epoch": 3,
    }
    assert AcceptInvitationArguments.model_validate_json(arguments.model_dump_json()) == arguments


ACCEPT_WIRE = {"place": {"kind": "room", "id": "r-1"}, "invitation": "i-1", "epoch": 3}
DECLINE_WIRE = ACCEPT_WIRE | {"reason": "busy"}


@pytest.mark.parametrize(
    "fields",
    [{"epoch": -1}, {"epoch": 1.0}, {"invitation": ""}, {"extra": 1}],
    ids=["negative-epoch", "fractional-epoch", "empty-invitation", "unknown-member"],
)
def test_accept_invitation_arguments_with_a_malformed_field_are_refused(
    fields: dict[str, Any],
) -> None:
    wire = ACCEPT_WIRE | fields

    wire_text = json.dumps(wire)

    with pytest.raises(ValidationError):
        AcceptInvitationArguments.model_validate_json(wire_text)


def test_decline_invitation_arguments_take_the_place_from_the_standing_with_reason_busy() -> None:
    standing = Standing(place=PLACE, state=StandingState.INVITED, invitation=INVITATION)
    assert standing.invitation is not None

    arguments = DeclineInvitationArguments(
        place=standing.place,
        invitation=standing.invitation.id,
        epoch=standing.invitation.epoch,
        reason=DeclineInvitationReason.BUSY,
    )

    assert json.loads(arguments.model_dump_json()) == DECLINE_WIRE
    assert DeclineInvitationArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize(
    "wire",
    [
        {key: value for key, value in DECLINE_WIRE.items() if key != "reason"},
        DECLINE_WIRE | {"reason": "full"},
        DECLINE_WIRE | {"reason": "BUSY"},
        DECLINE_WIRE | {"reason": None},
        DECLINE_WIRE | {"epoch": -1},
        DECLINE_WIRE | {"invitation": ""},
        DECLINE_WIRE | {"extra": 1},
    ],
    ids=[
        "no-reason",
        "other-reason",
        "upper-case-reason",
        "null-reason",
        "negative-epoch",
        "empty-invitation",
        "unknown-member",
    ],
)
def test_decline_invitation_arguments_with_a_malformed_field_are_refused(
    wire: dict[str, Any],
) -> None:
    wire_text = json.dumps(wire)

    with pytest.raises(ValidationError):
        DeclineInvitationArguments.model_validate_json(wire_text)


FIGURE_WIRE = {"url": "https://figures.example/a.glb", "sha256": "ab12" * 16}


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"figure": FIGURE_WIRE},
        {"voices": ["nova-2.hd_x"]},
        {"voices": ["en_US-example-medium"]},
        {"voices": ["nova", "alloy", "echo"]},
        {"voices": ["b", "a", "c"]},
        {"voices": ["Nova", "nova"]},
        {"voices": [f"v{number}" for number in range(8)]},
        {"voices": ["a" * 64]},
        {"figure": FIGURE_WIRE, "voices": ["nova"]},
    ],
    ids=[
        "neither",
        "figure-only",
        "one-voice",
        "voice-with-capitals",
        "voices-in-order",
        "order-not-sorted",
        "names-differing-in-case-only",
        "eight-voices",
        "voice-of-64-characters",
        "both",
    ],
)
def test_accept_invitation_arguments_take_a_figure_and_voices_in_order_or_neither(
    fields: dict[str, Any],
) -> None:
    arguments = AcceptInvitationArguments.model_validate_json(json.dumps(ACCEPT_WIRE | fields))

    dumped = json.loads(arguments.model_dump_json())
    assert dumped.get("voices") == fields.get("voices")
    assert ("voices" in dumped) == ("voices" in fields)
    assert ("figure" in dumped) == ("figure" in fields)
    assert AcceptInvitationArguments.model_validate_json(arguments.model_dump_json()) == arguments


@pytest.mark.parametrize(
    "url",
    [
        "http://figures.example/a.glb",
        "https://user@figures.example/a.glb",
        "https://user:secret@figures.example/a.glb",
        "https://figures.example/a.glb?v=1",
        "https://figures.example/a.glb#top",
        "/figures/a.glb",
        "figures.example/a.glb",
        "https:///a.glb",
        "https://ho st/a.glb",
        "https://ho\x00st/a.glb",
        "https://ho\tst/a.glb",
        "https://host/a\nb.glb",
        " https://host/a.glb",
        "https://%/a.glb",
        "https://evil.example\\.good.example/a.glb",
        "https://host/a\\b.glb",
        "https://host:abc/a.glb",
        "https://host:99999/a.glb",
        'https://ho"st/a.glb',
        "https://ho<st/a.glb",
        "https://ho>st/a.glb",
        "https://ho^st/a.glb",
        "https://ho`st/a.glb",
        "https://ho{st/a.glb",
        "https://ho|st/a.glb",
        "https://ho}st/a.glb",
    ],
    ids=[
        "http",
        "user",
        "user-password",
        "query",
        "fragment",
        "relative",
        "no-scheme",
        "no-host",
        "space-in-host",
        "nul-in-host",
        "tab",
        "newline",
        "leading-space",
        "percent-host",
        "backslash-in-host",
        "backslash-in-path",
        "non-numeric-port",
        "port-over-65535",
        "double-quote-in-host",
        "less-than-in-host",
        "greater-than-in-host",
        "caret-in-host",
        "backtick-in-host",
        "open-brace-in-host",
        "pipe-in-host",
        "close-brace-in-host",
    ],
)
def test_accept_invitation_arguments_refuse_a_figure_url_that_is_not_a_plain_https_address(
    url: str,
) -> None:
    wire_text = json.dumps(ACCEPT_WIRE | {"figure": FIGURE_WIRE | {"url": url}})

    with pytest.raises(ValidationError):
        AcceptInvitationArguments.model_validate_json(wire_text)


@pytest.mark.parametrize(
    "digest",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "", " " + "a" * 63],
    ids=["short", "long", "uppercase", "non-hex", "empty", "space"],
)
def test_accept_invitation_arguments_refuse_a_figure_digest_that_is_not_64_lowercase_hex(
    digest: str,
) -> None:
    wire_text = json.dumps(ACCEPT_WIRE | {"figure": FIGURE_WIRE | {"sha256": digest}})

    with pytest.raises(ValidationError):
        AcceptInvitationArguments.model_validate_json(wire_text)


@pytest.mark.parametrize(
    "fields",
    [
        {"voices": []},
        {"voices": [f"v{number}" for number in range(9)]},
        {"voices": ["nova", "nova"]},
        {"voices": ["nova", "alloy", "nova"]},
        {"voices": "nova"},
        {"voices": ["no va"]},
        {"voices": ["no/va"]},
        {"voices": [""]},
        {"voices": ["a" * 65]},
        {"voices": ["n\u00f6va"]},
        {"voices": ["nova\n"]},
        {"voices": ["nova", 1]},
        {"voice": "nova"},
        {"voice": "nova", "voices": ["nova"]},
    ],
    ids=[
        "empty-list",
        "nine-voices",
        "duplicate-voice",
        "duplicate-voice-apart",
        "bare-name",
        "space",
        "slash",
        "empty-name",
        "65-characters",
        "non-ascii-letter",
        "trailing-newline",
        "non-string-name",
        "old-voice-key",
        "old-voice-key-beside-voices",
    ],
)
def test_accept_invitation_arguments_refuse_voices_outside_the_form(
    fields: dict[str, Any],
) -> None:
    wire_text = json.dumps(ACCEPT_WIRE | fields)

    with pytest.raises(ValidationError):
        AcceptInvitationArguments.model_validate_json(wire_text)
