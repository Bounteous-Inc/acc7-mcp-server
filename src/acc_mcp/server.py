"""FastMCP server entry point.

`MCP_TRANSPORT` selects the transport:

    stdio  Claude launches the server per client; connection details come from
           that client's environment. Zero setup, single-tenant.
    http   (default) One long-running process serves many clients, each
           identifying its instance with X-ACC-* headers. This is the
           deployment path, so running it locally exercises the same code.
"""

from __future__ import annotations

from fastmcp import FastMCP

from .config import get_server_settings
from .tools import register_all

# Note: no load_dotenv() here on purpose. python-dotenv's default search walks
# up from this module's directory, which would pick up the developer's .env
# regardless of where the server was started from — precisely the silent
# fallback a multi-tenant server must not have. ServerSettings reads .env from
# the working directory, and under http connection details come from headers.

mcp = FastMCP(
    name="acc7-mcp",
    instructions=(
        "Read-only access to an Adobe Campaign Classic v7 instance, for "
        "migration inventory, schema audit and dependency mapping.\n\n"
        "Start with test_connection, then list_schemas to discover what the "
        "instance contains. Narrow with namespace='cus' to find custom work, "
        "use count_records to drop empty tables before spending calls on them, "
        "then get_schema_definition for structure. Everything is read-only; "
        "there is no write path."
    ),
)

register_all(mcp)


def main() -> None:
    """Console entry point. Transport comes from MCP_TRANSPORT."""
    settings = get_server_settings()
    if settings.mcp_transport == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="http",
            host=settings.mcp_http_host,
            port=settings.mcp_http_port,
        )


if __name__ == "__main__":
    main()
