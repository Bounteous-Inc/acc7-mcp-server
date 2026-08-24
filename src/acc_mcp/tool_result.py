"""One result shape for every tool, success or failure.

Errors are returned as data rather than raised, so the agent always receives
every field intact instead of a flattened string. Each carries enough to decide
what to do next without guessing:

    isError        did this fail
    isRetryable    would the identical call plausibly succeed later
    retryAfter     seconds to wait, when retryable
    errorType      stable machine-readable class
    message        what went wrong, in plain language
    retryGuidance  what the agent should actually do about it

Protocol-level `isError` is reserved for unhandled crashes — anything this
module produces is a handled outcome.
"""

from __future__ import annotations

from typing import Any

from .acc.transport import redact
from .acc.v7.errors import (
    AccError,
    AuthError,
    EntityKeyError,
    PermissionError_,
    QueryError,
    SchemaNotFoundError,
    SessionExpiredError,
    TransportError,
)
from .config import ConfigurationError

# errorType -> (isRetryable, retryAfterSeconds, retryGuidance)
_POLICY: dict[str, tuple[bool, int | None, str]] = {
    "ConfigurationError": (
        False,
        None,
        "Do not retry. The server is missing connection details — this is fixed "
        "by whoever configured the MCP client, not by changing tool arguments.",
    ),
    "AuthError": (
        False,
        None,
        "Do not retry. The credentials are wrong or the account cannot use the "
        "SOAP API. Report this to the user; retrying will fail identically.",
    ),
    "PermissionError_": (
        False,
        None,
        "Do not retry this call. The operator lacks rights on this object. Try a "
        "different schema, or report that the account needs additional rights.",
    ),
    "QueryError": (
        False,
        None,
        "Do not retry unchanged — correct the request first. Check the field and "
        "condition expressions against get_schema_definition, then call again.",
    ),
    "SchemaNotFoundError": (
        False,
        None,
        "Do not retry with this name. Call list_schemas to find the correct "
        "fully qualified schema name, then call again.",
    ),
    "EntityKeyError": (
        False,
        None,
        "Do not retry with this key. This schema is not addressable by entity "
        "key; use query_schema to read its records instead.",
    ),
    "SessionExpiredError": (
        True,
        0,
        "Retry immediately. The session expired and has been renewed; the same "
        "call should now succeed.",
    ),
    "TransportError": (
        True,
        5,
        "Retry after a short wait. If it fails repeatedly, the instance is "
        "unreachable — check the base URL and network with test_connection.",
    ),
    "AccError": (
        False,
        None,
        "Do not retry unchanged. Read the message for what the instance "
        "objected to, adjust the request, then try again.",
    ),
}

_DEFAULT_POLICY = (False, None, "Do not retry unchanged. Inspect the message.")


def ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a successful result."""
    return {"isError": False, **payload}


def error(exc: Exception, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Wrap a failure into the standard envelope."""
    error_type = type(exc).__name__

    if isinstance(exc, AccError):
        message = exc.friendly()
    elif isinstance(exc, ConfigurationError):
        message = str(exc)
    else:
        # Unexpected — still reported as data, but named honestly.
        error_type = f"Unexpected:{error_type}"
        message = redact(str(exc)) or "An unexpected error occurred."

    retryable, retry_after, guidance = _POLICY.get(
        type(exc).__name__, _DEFAULT_POLICY
    )

    envelope: dict[str, Any] = {
        "isError": True,
        "isRetryable": retryable,
        "errorType": error_type,
        "message": message,
        "retryGuidance": guidance,
    }
    if retry_after is not None:
        envelope["retryAfterSeconds"] = retry_after
    if context:
        envelope.update(context)
    return envelope


__all__ = [
    "ok",
    "error",
    "AccError",
    "AuthError",
    "ConfigurationError",
    "EntityKeyError",
    "PermissionError_",
    "QueryError",
    "SchemaNotFoundError",
    "SessionExpiredError",
    "TransportError",
]
