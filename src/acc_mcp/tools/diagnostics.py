"""Diagnostic tools."""

from __future__ import annotations

from typing import Any

from .. import tool_result
from ..deps import get_client


def register(mcp) -> None:
    @mcp.tool
    async def test_connection() -> dict[str, Any]:
        """Check that the Campaign instance is reachable and the connection
        credentials are valid.

        Returns instance URL, version and build number, resolved auth mode, the
        operator identity and round-trip latency. Run this first when something
        is failing: it makes no query, so a failure is unambiguously auth or
        connectivity rather than a bad request. Never returns tokens.

        Connection details come from the headers the MCP client was configured
        with, not from tool arguments.
        """
        try:
            client = await get_client()
            return tool_result.ok(await client.ping())
        except Exception as exc:  # noqa: BLE001 - reported as data, see tool_result
            return tool_result.error(exc)
