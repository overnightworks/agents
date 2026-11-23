"""What a listening member session wakes its runner with, in the order the app sent it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from mcp.client.subscriptions import (
    ResourceUpdated,
    Subscription,
    SubscriptionLost,
    ToolsListChanged,
)

from agents.mcp_client.exchange import BearerAuth
from agents.mcp_client.fold import failure_of


@dataclass(frozen=True)
class ResourceWake:
    """The resource at ``uri`` changed and may need to be read again."""

    uri: str


@dataclass(frozen=True)
class ToolsWake:
    """The tools the app offers this member changed and may need to be listed again."""


type MemberWake = ResourceWake | ToolsWake


async def wakes_of(
    subscription: Subscription, auth: BearerAuth, on_tools_notice: Callable[[], None]
) -> AsyncIterator[MemberWake]:
    """The subscription's wakes; a tools notice is counted before its wake is yielded."""
    try:
        async for event in subscription:
            if isinstance(event, ToolsListChanged):
                on_tools_notice()
                yield ToolsWake()
            elif isinstance(event, ResourceUpdated):
                yield ResourceWake(event.uri)
    except SubscriptionLost as lost:
        raise failure_of(lost, auth.last_answer) from None
