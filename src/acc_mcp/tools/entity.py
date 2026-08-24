"""Full-entity retrieval, for dependency mapping."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field


def register(mcp) -> None:
    @mcp.tool
    async def get_entity(
        entity_key: Annotated[
            str,
            Field(
                description=(
                    "Entity key as 'schema|name', e.g. "
                    "'xtk:srcSchema|cus:loyalty', 'nms:deliveryMapping|mapRecipient'."
                )
            ),
        ],
        must_exist: Annotated[
            bool,
            Field(description="Fail if the entity is absent, rather than returning empty."),
        ] = True,
    ) -> dict[str, Any]:
        """Fetch the complete XML document for one entity.

        Where dependency edges live: link targets, extension relationships and
        referenced objects sit inside the entity body, not in the flat columns
        query_schema returns.

        Only works for schemas keyed by @name. xtk:workflow is keyed by
        @internalName and cannot be fetched this way — use query_schema for
        workflow metadata instead.
        """
        raise NotImplementedError
