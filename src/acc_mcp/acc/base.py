"""The stable internal interface every ACC version adapter implements.

Tools depend only on this protocol. A future v8 (REST) adapter drops in beside
`Acc7Client` without the MCP layer changing.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

SchemaForm = Literal["compiled", "source", "wsdl"]


@runtime_checkable
class AccAdapter(Protocol):
    async def ping(self) -> dict[str, Any]:
        """Authenticate and report instance identity + latency."""
        ...

    async def list_schemas(
        self,
        namespace: str | None = None,
        include_builtin: bool = True,
    ) -> list[dict[str, Any]]:
        """All schemas in the instance, built-in and custom."""
        ...

    async def get_schema_definition(self, schema: str, form: SchemaForm) -> str:
        """Raw XML for one schema, in the requested representation."""
        ...

    async def get_schema_inventory(self, schema: str) -> dict[str, Any]:
        """Compact structural inventory: fields, links, keys, enumerations."""
        ...

    async def query(
        self,
        schema: str,
        fields: list[str],
        where: str | None = None,
        order_by: list[str] | None = None,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read records. Returns `(rows, next_cursor)`."""
        ...

    async def count(self, schema: str, where: str | None = None) -> int:
        """Row count without transferring rows."""
        ...

    async def get_entity(self, entity_key: str, must_exist: bool = True) -> str:
        """Full XML document for one entity, keyed `schema|name`."""
        ...

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        ...
