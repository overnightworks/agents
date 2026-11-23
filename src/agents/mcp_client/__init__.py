"""What a runner sends an app's MCP endpoint; it needs the ``mcp-client`` extra.

No base module of ``agents`` imports this package, nor does ``agents.mcp_server``, so the base
keeps its two dependencies, pydantic and ruamel.yaml.
"""

from agents.mcp_client.skills import get_skill, list_skills

__all__ = ["get_skill", "list_skills"]
