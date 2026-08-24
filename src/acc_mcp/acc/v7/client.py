"""`Acc7Client` — the v7 SOAP implementation of `AccAdapter`.

One client instance per (instance, operator) connection. Each owns its own
session, so tenants never share tokens.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from ...config import ConnectionConfig, ServerSettings
from ..base import SchemaForm
from ..transport import Transport, TransportFailure, redact
from .auth import Authenticator, build_authenticator
from .envelopes import RESERVED_NAMESPACES, build_execute_query
from .errors import QueryError, SessionExpiredError, TransportError
from .parser import parse_count, parse_instance_test, parse_rows, raise_for_fault


_NAMESPACE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_SCHEMA_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}:[A-Za-z][A-Za-z0-9_]{0,63}$")


def _validate_schema_name(schema: str) -> None:
    """Reject anything that is not a `namespace:name` identifier.

    Schema names are interpolated into the queryDef, so this is both a guard
    and a much clearer failure than whatever fault ACC would return.
    """
    if not _SCHEMA_RE.match(schema.strip()):
        hint = (
            " Schema names are qualified, e.g. 'nms:recipient' — did you mean "
            f"'nms:{schema.strip()}'?"
            if ":" not in schema
            else ""
        )
        raise QueryError(
            f"Invalid schema name {schema!r}. Expected the form "
            f"'namespace:name', such as 'nms:recipient' or 'xtk:workflow'.{hint}"
        )


def _project_schema_row(row: dict[str, Any]) -> dict[str, Any]:
    """Trim an xtk:schema row to what a caller actually needs.

    ACC returns its whole "common set" regardless of what was selected —
    `_cs`, `created`, `createdBy-id`, `entitySchema`, `genAccessors`, `img`,
    `implements`, `lastModified`, `modifiedBy-id`, `xtkschema` — roughly four
    times the payload of the four fields we asked for. On a full instance that
    noise alone exhausts the response budget and truncates the inventory, so
    it is dropped here rather than shipped to the model.

    `md5` is kept: it changes when the schema does, which is free change
    detection and the natural cache key for Phase 2.
    """
    namespace = row.get("namespace", "")
    name = row.get("name", "")
    projected: dict[str, Any] = {
        "schema": f"{namespace}:{name}" if namespace and name else name,
        "namespace": namespace,
        "name": name,
        "label": row.get("label", ""),
        "mappingType": row.get("mappingType", ""),
        "md5": row.get("md5", ""),
    }
    if row.get("desc"):
        projected["desc"] = row["desc"]
    return projected


class Acc7Client:
    """Implements `AccAdapter` against Adobe Campaign Classic v7 over SOAP."""

    def __init__(
        self,
        connection: ConnectionConfig,
        settings: ServerSettings,
        transport: Transport,
        auth: Authenticator,
    ) -> None:
        self._connection = connection
        self._settings = settings
        self._transport = transport
        self._auth = auth

    @property
    def connection(self) -> ConnectionConfig:
        return self._connection

    # --- internals -----------------------------------------------------

    async def _call(
        self, soap_action: str, build_envelope: Callable[[str], str]
    ) -> str:
        """Authenticate if needed, POST, check for faults, return the body.

        Retries once through a transparent re-logon on session expiry.

        Takes a builder rather than a finished envelope because the session
        token is embedded in the SOAP body: after a re-logon the envelope has
        to be rebuilt with the new token, or we would replay the expired one.
        """
        await self._auth.ensure_session()
        try:
            body = await self._transport.post_soap(
                self._connection.soap_url,
                soap_action,
                build_envelope(self._auth.body_session_token()),
                headers=self._auth.auth_headers(),
            )
            raise_for_fault(body)
            return body
        except SessionExpiredError:
            await self._auth.relogon()
            body = await self._transport.post_soap(
                self._connection.soap_url,
                soap_action,
                build_envelope(self._auth.body_session_token()),
                headers=self._auth.auth_headers(),
            )
            raise_for_fault(body)  # a second expiry propagates
            return body
        except TransportFailure as exc:
            raise TransportError(redact(str(exc))) from exc

    async def _instance_info(self) -> dict[str, Any]:
        """Version, build and instance name from the unauthenticated /r/test."""
        try:
            body = await self._transport.get(self._connection.test_url)
        except TransportFailure:
            return {}
        return parse_instance_test(body)

    # --- AccAdapter ----------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        """Authenticate and report instance identity + latency.

        Deliberately makes no query: a failure here is unambiguously auth or
        connectivity. Never returns tokens.
        """
        started = time.perf_counter()
        await self._auth.ensure_session()
        logon_ms = round((time.perf_counter() - started) * 1000, 1)

        info = await self._instance_info()

        result: dict[str, Any] = {
            **self._connection.describe(),
            "session_established": True,
            "logon_latency_ms": logon_ms,
        }
        if info:
            result["instance"] = {
                "version": info.get("version"),
                "build": info.get("build"),
                "name": info.get("instance"),
                "status": info.get("status"),
                "server_time": info.get("date"),
            }
        session_info = getattr(self._auth, "session_info", None)
        if session_info:
            result["operator"] = session_info
        return result

    async def list_schemas(
        self,
        namespace: str | None = None,
        include_builtin: bool = True,
    ) -> list[dict[str, Any]]:
        """Every schema in the instance, optionally filtered by namespace.

        `@extendedSchema` is deliberately not selected: it does not exist on
        `xtk:schema` and asking for it fails the whole query with XTK-170036.
        Extension relationships come from get_schema_definition(form="source").
        """
        conditions: list[str] = []
        if namespace:
            cleaned = namespace.strip().rstrip(":")
            if not _NAMESPACE_RE.match(cleaned):
                raise QueryError(
                    f"Invalid namespace {namespace!r}. Expected a short "
                    "alphanumeric prefix such as 'cus', 'nms' or 'xtk'."
                )
            conditions.append(f"@namespace = '{cleaned}'")
        if not include_builtin:
            reserved = ", ".join(f"'{ns}'" for ns in RESERVED_NAMESPACES)
            conditions.append(f"@namespace NOT IN ({reserved})")

        where = " AND ".join(conditions) if conditions else None
        # Ask for one more than the cap so truncation is detectable.
        limit = self._settings.acc_max_rows + 1

        body = await self._call(
            "xtk:queryDef#ExecuteQuery",
            lambda token: build_execute_query(
                token,
                "xtk:schema",
                ["@name", "@namespace", "@label", "@mappingType"],
                where=where,
                order_by=["@namespace", "@name"],
                line_count=limit,
                start_line=0,
            ),
        )
        return [_project_schema_row(row) for row in parse_rows(body)]

    async def get_schema_definition(self, schema: str, form: SchemaForm) -> str:
        raise NotImplementedError

    async def query(
        self,
        schema: str,
        fields: list[str],
        where: str | None = None,
        order_by: list[str] | None = None,
        page_size: int | None = None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        raise NotImplementedError

    async def count(self, schema: str, where: str | None = None) -> int:
        """Row count via `operation="count"` — no rows transferred."""
        _validate_schema_name(schema)
        body = await self._call(
            "xtk:queryDef#ExecuteQuery",
            lambda token: build_execute_query(
                token, schema, [], where=where, operation="count"
            ),
        )
        return parse_count(body)

    async def get_entity(self, entity_key: str, must_exist: bool = True) -> str:
        raise NotImplementedError

    async def aclose(self) -> None:
        await self._transport.aclose()


def build_client(connection: ConnectionConfig, settings: ServerSettings) -> Acc7Client:
    """Wire transport + authenticator into a client for one connection."""
    transport = Transport(settings)
    auth = build_authenticator(connection, transport)
    return Acc7Client(connection, settings, transport, auth)
