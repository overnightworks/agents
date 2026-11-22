"""What the calling standard decides on facts an app supplies: every decision, pure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product
from typing import Literal

import pytest

from agents.calling import (
    Accepted,
    AcceptFacts,
    AcceptRefused,
    BookEntry,
    CallFacts,
    CallOutcome,
    Ended,
    Invited,
    InviteRefusal,
    OpenInvitation,
    OwnerAt,
    after_send_away,
    check_accept,
    check_decline,
    expired,
    invite,
    owner_left,
    send_away_allowed,
    standing,
)
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

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PLACE = PlaceRef(kind="room", id="r-1")


def open_invitation(**changes: object) -> OpenInvitation:
    fields: dict[str, object] = {
        "id": "i-1",
        "expires_at": NOW + timedelta(seconds=30),
        "owner": "owner-1",
        "bound_to": "seat-1",
        "inviter": "person-1",
    }
    return OpenInvitation.model_validate(fields | changes)


def entry(epoch: int = 3, invitation: OpenInvitation | None = None) -> BookEntry:
    return BookEntry(epoch=epoch, invitation=invitation)


def call_facts(**changes: object) -> CallFacts:
    fields: dict[str, object] = {
        "inviter": "person-1",
        "inviter_present": True,
        "inviter_is_member": True,
        "agent_listens": True,
        "agent_seated": False,
        "owner": "owner-1",
        "owner_at": OwnerAt.AT_BINDING,
        "bound_to": "seat-1",
        "seat_free": True,
    }
    return CallFacts.model_validate(fields | changes)


def accept_facts(**changes: object) -> AcceptFacts:
    fields: dict[str, object] = {
        "holds_lease": True,
        "owner": "owner-1",
        "owner_at": OwnerAt.AT_BINDING,
        "agent_seated": False,
        "seat_free": True,
    }
    return AcceptFacts.model_validate(fields | changes)


def acceptance(invitation: str = "i-1", epoch: int = 3) -> AcceptInvitationArguments:
    return AcceptInvitationArguments(place=PLACE, invitation=invitation, epoch=epoch)


def decline(invitation: str = "i-1", epoch: int = 3) -> DeclineInvitationArguments:
    return DeclineInvitationArguments(
        place=PLACE, invitation=invitation, epoch=epoch, reason=DeclineInvitationReason.BUSY
    )


def standing_call(
    held: Literal[StandingState.ACTIVE, StandingState.PAUSED] | None,
    book: BookEntry | None,
    *,
    serves_view: bool = False,
    parent: ParentPlace | None = None,
) -> Standing | None:
    return standing(book, PLACE, held=held, serves_view=serves_view, parent=parent, now=NOW)


FAILING_FACT = {
    InviteRefusal.NOT_PRESENT: {"inviter_present": False},
    InviteRefusal.NOT_A_MEMBER: {"inviter_is_member": False},
    InviteRefusal.ALREADY_THERE: {"agent_seated": True},
    InviteRefusal.NOT_LISTENING: {"agent_listens": False},
    InviteRefusal.OWNER_ABSENT: {"owner_at": OwnerAt.ABSENT},
    InviteRefusal.OWNER_AWAY: {"owner_at": OwnerAt.AWAY},
    InviteRefusal.OWNER_ELSEWHERE: {"owner_at": OwnerAt.ELSEWHERE},
    InviteRefusal.NO_FREE_SEAT: {"seat_free": False},
}


def call_with_failing(*refusals: InviteRefusal) -> Invited | InviteRefusal:
    facts: dict[str, object] = {}
    for refusal in refusals:
        facts |= FAILING_FACT.get(refusal, {})
    book = (
        entry(invitation=open_invitation(inviter="person-2"))
        if InviteRefusal.INVITED_BY_ANOTHER in refusals
        else None
    )
    return invite(book, call_facts(**facts), invitation_id="i-new", now=NOW)


@pytest.mark.parametrize("refusal", list(InviteRefusal))
def test_a_call_is_refused_for_the_one_fact_that_fails_calling_1_to_6(
    refusal: InviteRefusal,
) -> None:
    assert call_with_failing(refusal) == refusal


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (InviteRefusal.NOT_PRESENT, InviteRefusal.NOT_A_MEMBER),
        (InviteRefusal.NOT_A_MEMBER, InviteRefusal.ALREADY_THERE),
        (InviteRefusal.ALREADY_THERE, InviteRefusal.NOT_LISTENING),
        (InviteRefusal.NOT_LISTENING, InviteRefusal.INVITED_BY_ANOTHER),
        (InviteRefusal.INVITED_BY_ANOTHER, InviteRefusal.OWNER_ABSENT),
        (InviteRefusal.INVITED_BY_ANOTHER, InviteRefusal.OWNER_AWAY),
        (InviteRefusal.INVITED_BY_ANOTHER, InviteRefusal.OWNER_ELSEWHERE),
        (InviteRefusal.INVITED_BY_ANOTHER, InviteRefusal.NO_FREE_SEAT),
        (InviteRefusal.OWNER_ABSENT, InviteRefusal.NO_FREE_SEAT),
        (InviteRefusal.OWNER_AWAY, InviteRefusal.NO_FREE_SEAT),
        (InviteRefusal.OWNER_ELSEWHERE, InviteRefusal.NO_FREE_SEAT),
    ],
)
def test_the_first_refusal_in_the_ruled_order_wins_calling_1_to_6(
    first: InviteRefusal, second: InviteRefusal
) -> None:
    assert call_with_failing(second, first) == first


def test_the_same_inviters_open_invitation_is_returned_unchanged_calling_5() -> None:
    book = entry(epoch=2, invitation=open_invitation())

    result = invite(book, call_facts(), invitation_id="i-new", now=NOW)

    assert result == Invited(
        entry=book,
        invitation=Invitation(id="i-1", epoch=2, expires_at=NOW + timedelta(seconds=30)),
    )


def test_the_same_inviters_open_invitation_stands_whatever_the_owner_and_the_seat_say() -> None:
    book = entry(invitation=open_invitation())
    facts = call_facts(owner_at=OwnerAt.ABSENT, seat_free=False)

    result = invite(book, facts, invitation_id="i-new", now=NOW)

    assert isinstance(result, Invited)
    assert result.entry == book


def test_the_same_inviters_open_invitation_is_still_refused_when_the_agent_does_not_listen() -> (
    None
):
    book = entry(invitation=open_invitation())

    result = invite(book, call_facts(agent_listens=False), invitation_id="i-new", now=NOW)

    assert result == InviteRefusal.NOT_LISTENING


@pytest.mark.parametrize("inviter", ["person-1", "person-2"])
@pytest.mark.parametrize("remaining_seconds", [0, -1])
def test_an_invitation_that_is_no_longer_open_counts_as_none_calling_8(
    inviter: str, remaining_seconds: int
) -> None:
    lapsed = open_invitation(inviter=inviter, expires_at=NOW + timedelta(seconds=remaining_seconds))

    result = invite(entry(invitation=lapsed), call_facts(), invitation_id="i-new", now=NOW)

    assert isinstance(result, Invited)
    assert result.invitation.id == "i-new"


@pytest.mark.parametrize(("book", "epoch"), [(None, 0), (entry(epoch=4), 4)])
def test_a_new_invitation_lasts_invitation_seconds_and_keeps_the_epoch_calling_6(
    book: BookEntry | None, epoch: int
) -> None:
    expires_at = NOW + timedelta(seconds=INVITATION_SECONDS)
    opened = OpenInvitation(
        id="i-new", expires_at=expires_at, owner="owner-1", bound_to="seat-1", inviter="person-1"
    )

    result = invite(book, call_facts(), invitation_id="i-new", now=NOW)

    assert result == Invited(
        entry=BookEntry(epoch=epoch, invitation=opened),
        invitation=Invitation(id="i-new", epoch=epoch, expires_at=expires_at),
    )


@pytest.mark.parametrize(
    ("book", "arguments", "facts"),
    [
        pytest.param(None, acceptance(), accept_facts(), id="no row"),
        pytest.param(entry(), acceptance(), accept_facts(), id="no invitation"),
        pytest.param(
            entry(invitation=open_invitation()), acceptance("i-0"), accept_facts(), id="other id"
        ),
        pytest.param(
            entry(invitation=open_invitation()),
            acceptance(epoch=2),
            accept_facts(),
            id="other epoch",
        ),
        pytest.param(
            entry(invitation=open_invitation()),
            acceptance(),
            accept_facts(owner="owner-2"),
            id="other owner",
        ),
        pytest.param(
            entry(invitation=open_invitation()),
            acceptance(),
            accept_facts(agent_seated=True),
            id="already seated",
        ),
        pytest.param(
            entry(invitation=open_invitation()),
            acceptance(),
            accept_facts(holds_lease=False),
            id="no lease",
        ),
        pytest.param(
            entry(invitation=open_invitation(expires_at=NOW)),
            acceptance(),
            accept_facts(),
            id="expired",
        ),
        pytest.param(
            entry(invitation=open_invitation()),
            acceptance(),
            accept_facts(owner_at=OwnerAt.ABSENT, seat_free=False, holds_lease=False),
            id="stale before owner and seat",
        ),
    ],
)
def test_a_stale_accept_changes_nothing_calling_7(
    book: BookEntry | None, arguments: AcceptInvitationArguments, facts: AcceptFacts
) -> None:
    result = check_accept(book, arguments, facts, now=NOW)

    assert result == AcceptRefused(refusal=AcceptInvitationRefusal.STALE_INVITATION, ended=None)


@pytest.mark.parametrize(
    ("facts", "refusal", "outcome"),
    [
        pytest.param(
            accept_facts(owner_at=OwnerAt.ELSEWHERE),
            AcceptInvitationRefusal.TWO_SEATS,
            CallOutcome.OWNER_AWAY,
            id="two seats",
        ),
        pytest.param(
            accept_facts(owner_at=OwnerAt.ELSEWHERE, seat_free=False),
            AcceptInvitationRefusal.TWO_SEATS,
            CallOutcome.OWNER_AWAY,
            id="two seats before busy",
        ),
        pytest.param(
            accept_facts(owner_at=OwnerAt.ABSENT),
            AcceptInvitationRefusal.SPONSOR_AWAY,
            CallOutcome.OWNER_AWAY,
            id="owner absent",
        ),
        pytest.param(
            accept_facts(owner_at=OwnerAt.AWAY),
            AcceptInvitationRefusal.SPONSOR_AWAY,
            CallOutcome.OWNER_AWAY,
            id="owner away",
        ),
        pytest.param(
            accept_facts(seat_free=False),
            AcceptInvitationRefusal.BUSY,
            CallOutcome.BUSY,
            id="no free seat",
        ),
    ],
)
def test_a_refused_accept_ends_the_call_with_its_outcome_calling_7(
    facts: AcceptFacts, refusal: AcceptInvitationRefusal, outcome: CallOutcome
) -> None:
    book = entry(epoch=3, invitation=open_invitation())

    result = check_accept(book, acceptance(), facts, now=NOW)

    assert result == AcceptRefused(
        refusal=refusal,
        ended=Ended(entry=entry(epoch=3), inviter="person-1", outcome=outcome, at=NOW),
    )


def test_an_accept_clears_the_invitation_and_keeps_the_epoch_calling_7() -> None:
    book = entry(epoch=3, invitation=open_invitation())

    result = check_accept(book, acceptance(), accept_facts(), now=NOW)

    assert result == Accepted(entry=entry(epoch=3))


@pytest.mark.parametrize(
    ("book", "arguments"),
    [
        pytest.param(None, decline(), id="no row"),
        pytest.param(entry(), decline(), id="no invitation"),
        pytest.param(entry(invitation=open_invitation()), decline("i-0"), id="other id"),
        pytest.param(entry(invitation=open_invitation()), decline(epoch=2), id="other epoch"),
        pytest.param(entry(invitation=open_invitation(expires_at=NOW)), decline(), id="expired"),
    ],
)
def test_a_stale_decline_is_refused_calling_31(
    book: BookEntry | None, arguments: DeclineInvitationArguments
) -> None:
    assert check_decline(book, arguments, now=NOW) == DeclineInvitationRefusal.STALE_INVITATION


def test_a_decline_ends_the_call_busy_and_keeps_the_epoch_calling_30() -> None:
    book = entry(epoch=3, invitation=open_invitation())

    result = check_decline(book, decline(), now=NOW)

    assert result == Ended(
        entry=entry(epoch=3), inviter="person-1", outcome=CallOutcome.BUSY, at=NOW
    )


@pytest.mark.parametrize("seconds_after_expiry", [0, 5])
def test_an_invitation_that_is_no_longer_open_ends_with_no_answer_at_its_expiry_calling_8(
    seconds_after_expiry: int,
) -> None:
    lapsed = open_invitation(expires_at=NOW - timedelta(seconds=seconds_after_expiry))
    book = entry(epoch=3, invitation=lapsed)

    assert expired(book, now=NOW) == Ended(
        entry=entry(epoch=3),
        inviter="person-1",
        outcome=CallOutcome.NO_ANSWER,
        at=lapsed.expires_at,
    )


@pytest.mark.parametrize(
    "book", [None, entry(), entry(invitation=open_invitation())], ids=["no row", "none", "open"]
)
def test_an_open_invitation_or_none_does_not_expire_calling_8(book: BookEntry | None) -> None:
    assert expired(book, now=NOW) is None


def test_an_open_invitation_ends_with_owner_away_when_its_owner_leaves_calling_6() -> None:
    book = entry(epoch=3, invitation=open_invitation())

    assert owner_left(book, now=NOW) == Ended(
        entry=entry(epoch=3), inviter="person-1", outcome=CallOutcome.OWNER_AWAY, at=NOW
    )


@pytest.mark.parametrize(
    "book",
    [None, entry(), entry(invitation=open_invitation(expires_at=NOW))],
    ids=["no row", "no invitation", "expired"],
)
def test_an_owner_leaving_ends_no_invitation_that_is_not_open_calling_6(
    book: BookEntry | None,
) -> None:
    assert owner_left(book, now=NOW) is None


@pytest.mark.parametrize(
    ("member", "owner", "right"), [c for c in product([False, True], repeat=3) if any(c)]
)
def test_a_present_sender_with_any_one_title_may_send_away_calling_9(
    member: bool, owner: bool, right: bool
) -> None:
    assert send_away_allowed(
        sender_present=True, sender_is_member=member, sender_is_owner=owner, place_right=right
    )


def test_a_present_sender_with_no_title_may_not_send_away_calling_9() -> None:
    assert not send_away_allowed(
        sender_present=True, sender_is_member=False, sender_is_owner=False, place_right=False
    )


@pytest.mark.parametrize(("member", "owner", "right"), list(product([False, True], repeat=3)))
def test_an_absent_sender_may_not_send_away_whatever_their_titles_calling_9(
    member: bool, owner: bool, right: bool
) -> None:
    assert not send_away_allowed(
        sender_present=False, sender_is_member=member, sender_is_owner=owner, place_right=right
    )


@pytest.mark.parametrize(
    ("book", "epoch"),
    [
        (entry(epoch=2, invitation=open_invitation()), 3),
        (entry(epoch=0), 1),
        (None, 1),
    ],
)
def test_a_send_away_raises_the_epoch_and_drops_the_invitation_calling_10(
    book: BookEntry | None, epoch: int
) -> None:
    assert after_send_away(book) == BookEntry(epoch=epoch, invitation=None)


@pytest.mark.parametrize("held", [StandingState.ACTIVE, StandingState.PAUSED])
@pytest.mark.parametrize("book", [None, entry(invitation=open_invitation())])
def test_a_held_place_stands_as_its_state_with_no_invitation_calling_27(
    held: StandingState, book: BookEntry | None
) -> None:
    assert standing_call(held, book) == Standing(place=PLACE, state=held)


def test_an_open_invitation_stands_as_invited_calling_27() -> None:
    book = entry(epoch=2, invitation=open_invitation())

    assert standing_call(None, book) == Standing(
        place=PLACE,
        state=StandingState.INVITED,
        invitation=Invitation(id="i-1", epoch=2, expires_at=NOW + timedelta(seconds=30)),
    )


@pytest.mark.parametrize(
    "book",
    [None, entry(), entry(invitation=open_invitation(expires_at=NOW))],
    ids=["no row", "no invitation", "expired"],
)
def test_a_place_neither_held_nor_invited_has_no_standing_calling_27(
    book: BookEntry | None,
) -> None:
    assert standing_call(None, book) is None


@pytest.mark.parametrize("held", [None, StandingState.ACTIVE])
def test_a_standing_carries_serves_view_and_parent_calling_27(
    held: StandingState | None,
) -> None:
    parent = ParentPlace(app="https://parent.example/mcp", place=PlaceRef(kind="hall", id="h-1"))

    result = standing_call(
        held, entry(invitation=open_invitation()), serves_view=True, parent=parent
    )

    assert result is not None
    assert (result.serves_view, result.parent) == (True, parent)


def test_a_book_entry_with_an_open_invitation_round_trips_through_json() -> None:
    book = entry(epoch=2, invitation=open_invitation())

    assert BookEntry.model_validate_json(book.model_dump_json()) == book
