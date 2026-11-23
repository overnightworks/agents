"""Reading an app's skills over an SDK session: ``skills/list`` and ``skills/get``.

The functions write no log line and catch nothing: an error answer or a lost transport leaves as
the SDK raises it, and a result that breaks a rule of ``agents.skills`` as ``ValidationError``.
Paging is the caller's: it passes ``SkillListing.next_cursor`` back until it is absent.
"""

from __future__ import annotations

from typing import Literal

from mcp import ClientSession
from mcp_types import Request, RequestParams

from agents.skills import (
    SKILLS_GET_METHOD,
    SKILLS_LIST_METHOD,
    SkillAnswer,
    SkillListing,
    SkillsGetParams,
    SkillsListParams,
)


class _ListParams(RequestParams, SkillsListParams):
    """The params record with the ``_meta`` member the SDK stamps on every request."""


class _GetParams(RequestParams, SkillsGetParams):
    """The params record with the ``_meta`` member the SDK stamps on every request."""


class SkillsListRequest(Request[_ListParams, Literal["skills/list"]]):
    method: Literal["skills/list"] = SKILLS_LIST_METHOD
    params: _ListParams


class SkillsGetRequest(Request[_GetParams, Literal["skills/get"]]):
    method: Literal["skills/get"] = SKILLS_GET_METHOD
    params: _GetParams


async def list_skills(session: ClientSession, cursor: str | None = None) -> SkillListing:
    """Read one page of the app's skills."""
    request = SkillsListRequest(params=_ListParams(cursor=cursor))
    return await session.send_request(request, SkillListing)


async def get_skill(session: ClientSession, uri: str) -> SkillAnswer:
    """Read the entry of the skill whose ``SKILL.md`` is at ``uri``."""
    request = SkillsGetRequest(params=_GetParams(uri=uri))
    return await session.send_request(request, SkillAnswer)
