"""MCP tool definitions. Each module registers its tools on the FastMCP app."""

from . import diagnostics, discovery, entity, query

__all__ = ["diagnostics", "discovery", "entity", "query"]


def register_all(mcp) -> None:
    """Register every Phase 1 tool on the given FastMCP instance."""
    diagnostics.register(mcp)
    discovery.register(mcp)
    query.register(mcp)
    entity.register(mcp)
