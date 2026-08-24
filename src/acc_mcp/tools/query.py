"""Record-reading tools."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .. import tool_result
from ..deps import get_client


def register(mcp) -> None:
    @mcp.tool
    async def query_schema(
        schema: Annotated[
            str,
            Field(description="Schema to read, e.g. 'xtk:workflow', 'nms:delivery'."),
        ],
        fields: Annotated[
            list[str],
            Field(
                description=(
                    "XPath-style field expressions, e.g. "
                    "['@internalName', '@label', '@state']."
                )
            ),
        ],
        where: Annotated[
            str | None,
            Field(description="XPath-style condition, e.g. \"@state = 11\"."),
        ] = None,
        order_by: Annotated[
            list[str] | None,
            Field(description="Sort expressions. Defaults to a stable sort if omitted."),
        ] = None,
        page_size: Annotated[
            int | None,
            Field(description="Rows per page. Server caps this."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Opaque cursor from a previous call's next_cursor."),
        ] = None,
    ) -> dict[str, Any]:
        """Read records from any schema.

        Intended for configuration objects — workflows, deliveries, folders —
        not bulk recipient extraction. Use count_records first if you only need
        a size.

        Results are capped; when truncated the response says so explicitly and
        returns a next_cursor. Paginate rather than raising page_size.
        """
        raise NotImplementedError

    @mcp.tool
    async def count_records(
        schema: Annotated[str, Field(description="Schema to count, e.g. 'nms:recipient'.")],
        where: Annotated[
            str | None,
            Field(description="Optional XPath-style condition."),
        ] = None,
    ) -> dict[str, Any]:
        """Count rows in a schema without transferring them.

        Cheap — the database counts and returns a single number, so this is the
        right way to size something before deciding whether to read it. Use it
        to size the migration, to spot dead objects (a custom table with 0 rows
        is one you do not migrate), and to reconcile before and after.

        Prefer this over calling query_schema and counting the rows yourself:
        that transfers every record and may be truncated, giving a wrong total.
        """
        try:
            client = await get_client()
            total = await client.count(schema, where=where)
            return tool_result.ok({"schema": schema, "where": where, "count": total})
        except Exception as exc:  # noqa: BLE001 - reported as data, see tool_result
            return tool_result.error(exc, context={"schema": schema, "where": where})
