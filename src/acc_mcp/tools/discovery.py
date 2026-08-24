"""Discovery tools: what exists in the instance, and how it is structured."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .. import tool_result
from ..config import get_server_settings
from ..deps import get_client
from ..formatting import cap_rows


def register(mcp) -> None:
    @mcp.tool
    async def list_schemas(
        namespace: Annotated[
            str | None,
            Field(description="Filter to one namespace, e.g. 'cus' for custom schemas."),
        ] = None,
        include_builtin: Annotated[
            bool,
            Field(description="Include Adobe's built-in schemas (xtk, nms, nl, ncm)."),
        ] = True,
    ) -> dict[str, Any]:
        """List every schema (table) in the Campaign instance, built-in and custom.

        The discovery entry point — call this first to learn what the instance
        contains. Nothing is hardcoded, so it works against any instance.

        For migration scoping, call with include_builtin=false to see only what
        this client built; that is the surface that actually has to be migrated.
        Follow up with count_records to drop empty tables before spending calls
        on get_schema_definition.

        Each row carries name, namespace, label and mappingType, plus an md5
        that changes when the schema does — useful for detecting drift.
        """
        try:
            client = await get_client()
            rows = await client.list_schemas(
                namespace=namespace, include_builtin=include_builtin
            )
            settings = get_server_settings()
            capped = cap_rows(
                rows, settings.acc_max_rows, settings.acc_max_response_chars
            )
            # Summarise over the full result, not the capped slice — deriving
            # it from `rows` after truncation would report a subset of the
            # namespaces as if it were all of them.
            return tool_result.ok(
                {
                    **capped,
                    "total_available": len(rows),
                    "namespaces": sorted(
                        {row.get("namespace", "") for row in rows} - {""}
                    ),
                    "filter": {
                        "namespace": namespace,
                        "include_builtin": include_builtin,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001 - reported as data, see tool_result
            return tool_result.error(exc)

    @mcp.tool
    async def get_schema_definition(
        schema: Annotated[
            str,
            Field(description="Fully qualified schema name, e.g. 'nms:recipient'."),
        ],
        form: Annotated[
            Literal["compiled", "source", "wsdl"],
            Field(
                description=(
                    "'source' = what this client customised, including "
                    "extendedSchema; 'compiled' = effective runtime structure "
                    "with joins, keys and indexes; 'wsdl' = SOAP method "
                    "signatures only."
                )
            ),
        ] = "compiled",
    ) -> dict[str, Any]:
        """Fetch the structure of one schema as raw XML.

        Use form='source' to see what a client added on top of a built-in
        schema — this is what distinguishes an extension from a new table.
        Use form='compiled' for the effective structure: fields, types, keys,
        indexes and the <join> elements that reveal schema-to-schema links.

        Returns XML as a string; element nesting carries meaning, so it is not
        flattened to JSON.
        """
        raise NotImplementedError
