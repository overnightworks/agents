"""What the calling standard decides, on facts an app supplies, written once.

An app calls an agent to a place and sends it away by the same rules as every other app
(``docs/calling.md``). This module holds those decisions and nothing else: it keeps no state,
reads no clock, store, token or realm, and performs no I/O. The app supplies the facts (who is
present, where the owner is, whether a seat is free), the time (``now``) and the invitation's id,
keeps each agent's row (``BookEntry``) in its own store and writes the entry a decision returns,
in the same step as its seats. A binding is an opaque, non-empty text the app chooses, compared by
equality only. An invitation is open while ``now < expires_at``.

The refusals and outcomes here are this module's own vocabulary; an app maps them onto its own.
The module imports ``agents.member`` and pydantic only, so an app's core may import it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, Field

from agents.member import (
    INVITATION_SECONDS,
    AcceptInvitationArguments,
    AcceptInvitationRefusal,
    DeclineInvitationArguments,
    DeclineInvitationReason,
    DeclineInvitationRefusal,
    Invitation,
    ParentPlace,
    PlaceRef,
    Standing,
    StandingState,
)
from agents.turn import NonEmptyUnicodeText, StrictModel

__all__ = [
    "AcceptFacts",
    "AcceptRefused",
    "Accepted",
    "BookEntry",
    "CallFacts",
    "CallOutcome",
    "Ended",
    "InviteRefusal",
    "Invited",
    "OpenInvitation",
    "OwnerAt",
    "after_send_away",
    "check_accept",
    "check_decline",
    "expired",
    "invite",
    "owner_left",
    "send_away_allowed",
    "standing",
]


class OpenInvitation(StrictModel):
    """An invitation as an app stores it: ``owner`` is the owner's account subject, ``bound_to``
    where the app judges the owner's presence and ``inviter`` who called, the only one told the
    outcome."""

    id: NonEmptyUnicodeText
    expires_at: AwareDatetime
    owner: NonEmptyUnicodeText
    bound_to: NonEmptyUnicodeText
    inviter: NonEmptyUnicodeText


class BookEntry(StrictModel):
    """One agent's row at one place, which the app keys and stores. ``epoch`` is the place's
    removal epoch for that agent; a missing row counts as epoch 0."""

    epoch: int = Field(ge=0)
    invitation: OpenInvitation | None = None


class OwnerAt(StrEnum):
    """Where an owner is, as the app derives it from its presence rule: ``AT_BINDING`` present
    where the invitation is or will be bound, ``ABSENT`` not at the place, ``AWAY`` its seat lost
    its connection and waits to resume, ``ELSEWHERE`` present, but not on exactly that binding,
    such as on two seats."""

    AT_BINDING = "at_binding"
    ABSENT = "absent"
    AWAY = "away"
    ELSEWHERE = "elsewhere"


class CallFacts(StrictModel):
    """What an app reads when a person calls an agent: ``inviter_is_member`` is the fresh read of
    the agent-adders group at the call and ``agent_listens`` whether the agent's runner listens
    for invitations."""

    inviter: NonEmptyUnicodeText
    inviter_present: bool
    inviter_is_member: bool
    agent_listens: bool
    agent_seated: bool
    owner: NonEmptyUnicodeText
    owner_at: OwnerAt
    bound_to: NonEmptyUnicodeText
    seat_free: bool


class AcceptFacts(StrictModel):
    """What an app reads when an agent accepts: ``owner`` is the agent's owner as its lease now
    carries it."""

    holds_lease: bool
    owner: NonEmptyUnicodeText
    owner_at: OwnerAt
    agent_seated: bool
    seat_free: bool


class InviteRefusal(StrEnum):
    """Why a call is refused, one reason per fact."""

    NOT_PRESENT = "not_present"
    NOT_A_MEMBER = "not_a_member"
    ALREADY_THERE = "already_there"
    NOT_LISTENING = "not_listening"
    INVITED_BY_ANOTHER = "invited_by_another"
    OWNER_ABSENT = "owner_absent"
    OWNER_AWAY = "owner_away"
    OWNER_ELSEWHERE = "owner_elsewhere"
    NO_FREE_SEAT = "no_free_seat"


class CallOutcome(StrEnum):
    """How a call ended without a seat."""

    NO_ANSWER = "no_answer"
    BUSY = "busy"
    OWNER_AWAY = "owner_away"


class Invited(StrictModel):
    """A call that stands: ``entry`` is the row to store next."""

    entry: BookEntry
    invitation: Invitation


class Accepted(StrictModel):
    """An accept the app seats in the same write as it stores ``entry``."""

    entry: BookEntry


class Ended(StrictModel):
    """A call ended without a seat: ``entry`` is the row to store next, with its epoch and no
    invitation, and ``inviter`` is the one told ``outcome`` at ``at``."""

    entry: BookEntry
    inviter: NonEmptyUnicodeText
    outcome: CallOutcome
    at: AwareDatetime


class AcceptRefused(StrictModel):
    """An accept the app refuses with ``refusal``; ``ended`` is the call's end, ``None`` for a
    stale accept, which changes nothing."""

    refusal: AcceptInvitationRefusal
    ended: Ended | None = None


_OWNER_REFUSALS: Final = {
    OwnerAt.ABSENT: InviteRefusal.OWNER_ABSENT,
    OwnerAt.AWAY: InviteRefusal.OWNER_AWAY,
    OwnerAt.ELSEWHERE: InviteRefusal.OWNER_ELSEWHERE,
}

_ACCEPT_OWNER_REFUSALS: Final = {
    OwnerAt.ELSEWHERE: AcceptInvitationRefusal.TWO_SEATS,
    OwnerAt.ABSENT: AcceptInvitationRefusal.SPONSOR_AWAY,
    OwnerAt.AWAY: AcceptInvitationRefusal.SPONSOR_AWAY,
}

_ACCEPT_OUTCOMES: Final = {
    AcceptInvitationRefusal.TWO_SEATS: CallOutcome.OWNER_AWAY,
    AcceptInvitationRefusal.SPONSOR_AWAY: CallOutcome.OWNER_AWAY,
    AcceptInvitationRefusal.BUSY: CallOutcome.BUSY,
}

_DECLINE_OUTCOMES: Final = {DeclineInvitationReason.BUSY: CallOutcome.BUSY}


def _open_invitation(entry: BookEntry | None, now: datetime) -> OpenInvitation | None:
    if entry is None or entry.invitation is None or entry.invitation.expires_at <= now:
        return None
    return entry.invitation


def _epoch(entry: BookEntry | None) -> int:
    return 0 if entry is None else entry.epoch


def _invited(epoch: int, invitation: OpenInvitation) -> Invited:
    return Invited(
        entry=BookEntry(epoch=epoch, invitation=invitation),
        invitation=_invitation_view(epoch, invitation),
    )


def _invitation_view(epoch: int, invitation: OpenInvitation) -> Invitation:
    return Invitation(id=invitation.id, epoch=epoch, expires_at=invitation.expires_at)


def _call_refusal(
    facts: CallFacts, standing_invitation: OpenInvitation | None
) -> InviteRefusal | None:
    if not facts.inviter_present:
        return InviteRefusal.NOT_PRESENT
    if not facts.inviter_is_member:
        return InviteRefusal.NOT_A_MEMBER
    if facts.agent_seated:
        return InviteRefusal.ALREADY_THERE
    if not facts.agent_listens:
        return InviteRefusal.NOT_LISTENING
    if standing_invitation is not None and standing_invitation.inviter != facts.inviter:
        return InviteRefusal.INVITED_BY_ANOTHER
    return None


def invite(
    entry: BookEntry | None, facts: CallFacts, *, invitation_id: str, now: datetime
) -> Invited | InviteRefusal:
    """Decide a call (CALLING-1 to 6): the first refusal that holds wins, in the order of the
    ``InviteRefusal`` checks below; an open invitation by the same inviter is returned unchanged,
    otherwise the entry gets a new one that expires ``INVITATION_SECONDS`` after ``now``."""
    standing_invitation = _open_invitation(entry, now)
    refusal = _call_refusal(facts, standing_invitation)
    if refusal is not None:
        return refusal
    if standing_invitation is not None:
        return _invited(_epoch(entry), standing_invitation)
    owner_refusal = _OWNER_REFUSALS.get(facts.owner_at)
    if owner_refusal is not None:
        return owner_refusal
    if not facts.seat_free:
        return InviteRefusal.NO_FREE_SEAT
    opened = OpenInvitation(
        id=invitation_id,
        expires_at=now + timedelta(seconds=INVITATION_SECONDS),
        owner=facts.owner,
        bound_to=facts.bound_to,
        inviter=facts.inviter,
    )
    return _invited(_epoch(entry), opened)


def _ended(epoch: int, invitation: OpenInvitation, outcome: CallOutcome, at: datetime) -> Ended:
    return Ended(
        entry=BookEntry(epoch=epoch),
        inviter=invitation.inviter,
        outcome=outcome,
        at=at,
    )


def _accept_is_stale(
    epoch: int,
    invitation: OpenInvitation,
    acceptance: AcceptInvitationArguments,
    facts: AcceptFacts,
) -> bool:
    return (
        invitation.id != acceptance.invitation
        or epoch != acceptance.epoch
        or invitation.owner != facts.owner
        or facts.agent_seated
        or not facts.holds_lease
    )


def check_accept(
    entry: BookEntry | None,
    acceptance: AcceptInvitationArguments,
    facts: AcceptFacts,
    *,
    now: datetime,
) -> Accepted | AcceptRefused:
    """Decide an agent's accept (CALLING-7). A stale one changes nothing. Otherwise the owner and
    the seat are judged, and a refusal ends the call: owner away for ``two_seats`` and
    ``sponsor_away``, busy for ``busy``. An accept clears the invitation and keeps the epoch; the
    app seats the agent in the same write."""
    epoch = _epoch(entry)
    invitation = _open_invitation(entry, now)
    if invitation is None or _accept_is_stale(epoch, invitation, acceptance, facts):
        return AcceptRefused(refusal=AcceptInvitationRefusal.STALE_INVITATION)
    refusal = _ACCEPT_OWNER_REFUSALS.get(facts.owner_at)
    if refusal is None and not facts.seat_free:
        refusal = AcceptInvitationRefusal.BUSY
    if refusal is None:
        return Accepted(entry=BookEntry(epoch=epoch))
    return AcceptRefused(
        refusal=refusal, ended=_ended(epoch, invitation, _ACCEPT_OUTCOMES[refusal], now)
    )


def check_decline(
    entry: BookEntry | None, decline: DeclineInvitationArguments, *, now: datetime
) -> Ended | DeclineInvitationRefusal:
    """Decide an agent's decline (CALLING-30, 31): a standing invitation ends the call at
    ``now`` with the outcome of the reason."""
    epoch = _epoch(entry)
    invitation = _open_invitation(entry, now)
    if invitation is None or invitation.id != decline.invitation or epoch != decline.epoch:
        return DeclineInvitationRefusal.STALE_INVITATION
    return _ended(epoch, invitation, _DECLINE_OUTCOMES[decline.reason], now)


def expired(entry: BookEntry | None, *, now: datetime) -> Ended | None:
    """End a lapsed invitation with no answer, at its ``expires_at`` (CALLING-8)."""
    if entry is None or entry.invitation is None or _open_invitation(entry, now) is not None:
        return None
    return _ended(entry.epoch, entry.invitation, CallOutcome.NO_ANSWER, entry.invitation.expires_at)


def owner_left(entry: BookEntry | None, *, now: datetime) -> Ended | None:
    """End an open invitation with owner away, at ``now``, when its owner leaves the place by the
    app's presence rule (CALLING-6)."""
    invitation = _open_invitation(entry, now)
    if invitation is None:
        return None
    return _ended(_epoch(entry), invitation, CallOutcome.OWNER_AWAY, now)


def send_away_allowed(
    *,
    sender_present: bool,
    sender_is_member: bool,
    sender_is_owner: bool,
    place_right: bool,
) -> bool:
    """Whether a person may send an agent away (CALLING-9): present at the place, and a fresh
    member of the agent-adders group, the agent's owner or holding a right the place's own rules
    give."""
    return sender_present and (sender_is_member or sender_is_owner or place_right)


def after_send_away(entry: BookEntry | None) -> BookEntry:
    """The row after a send-away (CALLING-10, 12): the epoch raised by one and no invitation."""
    return BookEntry(epoch=_epoch(entry) + 1)


def standing(
    entry: BookEntry | None,
    place: PlaceRef,
    *,
    held: StandingState | None,
    serves_view: bool,
    parent: ParentPlace | None,
    now: datetime,
) -> Standing | None:
    """The agent's standing at a place (CALLING-27): the state the place holds, ``ACTIVE`` or
    ``PAUSED``, with no invitation; otherwise ``INVITED`` while an invitation is open; otherwise
    none."""
    if held is not None:
        return Standing(place=place, state=held, serves_view=serves_view, parent=parent)
    invitation = _open_invitation(entry, now)
    if invitation is None:
        return None
    return Standing(
        place=place,
        state=StandingState.INVITED,
        invitation=_invitation_view(_epoch(entry), invitation),
        serves_view=serves_view,
        parent=parent,
    )
