"""What a runner sends an app's MCP endpoint; it needs the ``mcp-client`` extra.

No base module of ``agents`` imports this package, nor does ``agents.mcp_server``, so the base
keeps its two dependencies, pydantic and ruamel.yaml.
"""

from agents.mcp_client.session import (
    CONNECT_SECONDS,
    READ_SECONDS,
    AppDiscovery,
    MemberSession,
    open_member_session,
)
from agents.mcp_client.wakes import MemberWake, ResourceWake, ToolsWake

__all__ = [
    "CONNECT_SECONDS",
    "READ_SECONDS",
    "AppDiscovery",
    "MemberSession",
    "MemberWake",
    "ResourceWake",
    "ToolsWake",
    "open_member_session",
]
