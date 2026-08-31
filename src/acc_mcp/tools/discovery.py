"""Discovery tools: what exists in the instance, and how it is structured."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from .. import tool_result
from ..config import get_server_settings
from ..deps import get_client
from ..acc.v7.inventory import trim_inventory
from ..formatting import cap_document, cap_rows


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
            Literal["inventory", "compiled", "source", "wsdl"],
            Field(
                description=(
                    "'inventory' (default) = compact JSON structure: fields, "
                    "types, links, keys, enumerations. Use this unless you "
                    "need raw XML. 'source' = what this client customised, "
                    "including extendedSchema. 'compiled' = full effective "
                    "structure as XML; large schemas will be truncated. "
                    "'wsdl' = SOAP method signatures only."
                )
            ),
        ] = "inventory",
    ) -> dict[str, Any]:
        """Fetch the structure of one schema as raw XML.

        Use form='source' to see what a client added on top of a built-in
        schema — this is what distinguishes an extension from a new table.
        Use form='compiled' for the effective structure: fields, types, keys,
        indexes and the <join> elements that reveal schema-to-schema links.

        Default form='inventory' returns compact JSON — fields with types and
        lengths, links with their join conditions, keys, indexes and
        enumerations. That is what a migration audit needs, and it fits where
        raw XML does not: nms:delivery is 236k as XML but a few thousand as an
        inventory.

        The XML forms return a string rather than JSON because element nesting
        and ordering carry meaning. They can exceed the response budget and be
        truncated mid-element — check the `truncated` flag before parsing.
        """
        try:
            client = await get_client()
            settings = get_server_settings()

            if form == "inventory":
                inventory = await client.get_schema_inventory(schema)
                inventory = trim_inventory(inventory, settings.acc_max_response_chars)
                return tool_result.ok({"form": form, **inventory})

            xml = await client.get_schema_definition(schema, form)
            entity_key = (
                f"schemawsdl.jsp?schema={schema}"
                if form == "wsdl"
                else f"{'xtk:srcSchema' if form == 'source' else 'xtk:schema'}|{schema}"
            )
            return tool_result.ok(
                {
                    "schema": schema,
                    "form": form,
                    **cap_document(
                        xml,
                        settings.acc_max_response_chars,
                        entity_key=entity_key,
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - reported as data, see tool_result
            return tool_result.error(exc, context={"schema": schema, "form": form})
