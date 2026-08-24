"""Exception taxonomy for ACC faults, and their agent-facing messages.

SOAP faults arrive with HTTP 200, so every response is checked before use.
`SOP-330011` is a generic wrapper — the actionable text is in `<detail>`.
"""

from __future__ import annotations

import re


class AccError(Exception):
    """Base for every ACC-originated failure."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def friendly(self) -> str:
        """Actionable text safe to return to the agent. Never contains tokens."""
        from ..transport import redact

        parts = [self.message]
        if self.detail and self.detail.strip() not in self.message:
            parts.append(self.detail.strip())
        return redact(" — ".join(parts))


class AuthError(AccError):
    """Bad credentials. Fail fast, do not retry. e.g. XSV-350012."""

    def friendly(self) -> str:
        return (
            "Authentication failed: the instance rejected these credentials. "
            "Check the X-ACC-Login and X-ACC-Password headers this MCP client "
            "was configured with. Note that an Adobe ID cannot be used here — "
            "the SOAP API requires a native operator with a password."
        )


class SessionExpiredError(AccError):
    """Session no longer valid. Triggers exactly one transparent re-logon."""


class PermissionError_(AccError):
    """The API operator lacks rights on the requested schema or folder."""

    def friendly(self) -> str:
        from ..transport import redact

        return redact(
            f"The API operator lacks the rights needed for this call. {self.detail or self.message}"
        )


class QueryError(AccError):
    """Malformed query — unknown attribute, unparseable XPath. e.g. XTK-170036.

    The raw detail is preserved so the agent can correct itself.
    """


class SchemaNotFoundError(AccError):
    """The named schema does not exist on this instance. e.g. XFR-180000."""

    def friendly(self) -> str:
        # ACC reports this as a missing file and includes the server's internal
        # datakit path. The agent needs the schema name, not our filesystem.
        match = re.search(r"identifier '([^']+)'", self.detail or "")
        subject = f"Schema {match.group(1)!r}" if match else "The requested schema"
        return (
            f"{subject} does not exist on this instance. Call list_schemas to "
            "see what is available — names are case-sensitive and must be "
            "fully qualified, e.g. 'nms:recipient'."
        )


class EntityKeyError(AccError):
    """Entity key unusable for this schema.

    `GetEntityIfMoreRecent` resolves keys via `@name`; schemas without that
    attribute (notably `xtk:workflow`, which uses `@internalName`) cannot be
    fetched this way. See plan.md 4.5.
    """

    def friendly(self) -> str:
        return (
            "This schema cannot be fetched by entity key: the key resolver "
            "matches on @name, and this schema has no @name attribute "
            "(xtk:workflow uses @internalName). Use query_schema to read its "
            f"records instead. Original fault: {self.detail or self.message}"
        )


class TransportError(AccError):
    """Network failure, timeout, or exhausted retries."""

    def friendly(self) -> str:
        from ..transport import redact

        return redact(
            f"Could not reach the Campaign instance: {self.message}. "
            "Check ACC_BASE_URL and network reachability."
        )


# Ordered most-specific first; the first match wins.
_FAULT_PATTERNS: list[tuple[re.Pattern[str], type[AccError]]] = [
    (re.compile(r"XSV-350012|invalid login or password", re.I), AuthError),
    (re.compile(r"XSV-350008|session .*(expired|invalid)|invalid session", re.I), SessionExpiredError),
    (re.compile(r"XFR-180000", re.I), SchemaNotFoundError),
    (
        re.compile(r"attribute 'name' unknown.*schema '.*workflow|XSV-350000", re.I),
        EntityKeyError,
    ),
    (
        re.compile(r"access denied|not authoriz|insufficient rights|no read right", re.I),
        PermissionError_,
    ),
    (re.compile(r"XTK-170036|unknown \(see definition of schema|SOP-330024|SOP-330003", re.I), QueryError),
]


def classify_fault(fault_string: str, detail: str) -> AccError:
    """Map a SOAP fault onto the exception hierarchy above."""
    haystack = f"{fault_string}\n{detail}"
    for pattern, error_type in _FAULT_PATTERNS:
        if pattern.search(haystack):
            return error_type(fault_string.strip() or "ACC fault", detail=detail.strip())
    return AccError(fault_string.strip() or "ACC fault", detail=detail.strip())
