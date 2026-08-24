"""Session management.

Tokens live in memory on the client instance for the process lifetime and
nowhere else: no disk, no logs, no tool output. Expiry is fault-driven rather
than clock-driven, since the TTL is server-configurable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ...config import ConnectionConfig
from ..transport import Transport, TransportFailure, install_token_redaction
from .envelopes import build_logon
from .errors import TransportError
from .parser import parse_logon, parse_session_info, raise_for_fault

logger = logging.getLogger(__name__)


@dataclass
class SessionState:
    session_token: str | None = None    # -> Cookie: __sessiontoken=...
    security_token: str | None = None   # -> X-Security-Token: ...
    obtained_at: float | None = None

    def is_valid(self) -> bool:
        """Whether a session is currently held. Not an expiry check."""
        return bool(self.session_token)

    def clear(self) -> None:
        """Drop the tokens, forcing a re-logon on the next call."""
        self.session_token = None
        self.security_token = None
        self.obtained_at = None


class Authenticator(Protocol):
    """Auth strategy. `ACC_AUTH_MODE` selects the implementation."""

    async def ensure_session(self) -> None:
        """Log on if no session is held. Safe to call before every request."""
        ...

    async def relogon(self) -> None:
        """Force a fresh logon after a session-expiry fault."""
        ...

    def auth_headers(self) -> dict[str, str]:
        """Headers this strategy attaches to every authenticated call."""
        ...

    def body_session_token(self) -> str:
        """Value for the <sessiontoken> element in the SOAP body."""
        ...


class NativeLogonAuth:
    """`xtk:session#Logon` with a native operator login and password.

    Attaches both `X-Security-Token` and `Cookie: __sessiontoken=...` — the
    instance requires both on every authenticated call, not just writes.
    """

    mode = "native"

    def __init__(self, connection: ConnectionConfig, transport: Transport) -> None:
        self._connection = connection
        self._transport = transport
        self._state = SessionState()
        self._lock = asyncio.Lock()  # one re-logon under concurrent tool calls
        self._session_info: dict[str, Any] = {}

    # --- internals -----------------------------------------------------

    async def _logon(self) -> None:
        """Perform the Logon exchange and cache the resulting tokens."""
        self._connection.validate()
        envelope = build_logon(self._connection.login, self._connection.password)
        try:
            body = await self._transport.post_soap(
                self._connection.soap_url, "xtk:session#Logon", envelope
            )
        except TransportFailure as exc:
            raise TransportError(str(exc)) from exc

        raise_for_fault(body)  # AuthError on bad credentials
        session_token, security_token = parse_logon(body)

        # Register before storing, so nothing can log them in between.
        install_token_redaction(session_token, security_token)

        self._state = SessionState(
            session_token=session_token,
            security_token=security_token,
            obtained_at=time.time(),
        )
        self._session_info = parse_session_info(body)
        logger.info(
            "Logged on to %s as %s", self._connection.base_url, self._connection.login
        )

    # --- Authenticator -------------------------------------------------

    async def ensure_session(self) -> None:
        if self._state.is_valid():
            return
        async with self._lock:
            # Another coroutine may have logged on while we waited.
            if self._state.is_valid():
                return
            await self._logon()

    async def relogon(self) -> None:
        async with self._lock:
            self._state.clear()
            await self._logon()

    def auth_headers(self) -> dict[str, str]:
        if not self._state.is_valid():
            return {}
        return {
            "X-Security-Token": self._state.security_token or "",
            "Cookie": f"__sessiontoken={self._state.session_token}",
        }

    def body_session_token(self) -> str:
        return self._state.session_token or ""

    # --- diagnostics ---------------------------------------------------

    @property
    def session_info(self) -> dict[str, Any]:
        """Non-sensitive detail from the Logon response. Never tokens."""
        return dict(self._session_info)

    @property
    def obtained_at(self) -> float | None:
        return self._state.obtained_at


class OAuthS2SAuth:
    """IMS OAuth Server-to-Server bearer token.

    Not usable on this instance: it needs `nlserver config -setimsoauth:...`
    run on the Campaign host, which is an Adobe request on a hosted sandbox.
    Calls carry `Authorization: Bearer <token>` and leave <sessiontoken> empty.
    """

    mode = "oauth"

    def __init__(self, connection: ConnectionConfig, transport: Transport) -> None:
        self._connection = connection
        self._transport = transport

    async def ensure_session(self) -> None:
        raise NotImplementedError(
            "OAuth Server-to-Server is not implemented. Set ACC_AUTH_MODE=native."
        )

    async def relogon(self) -> None:
        raise NotImplementedError

    def auth_headers(self) -> dict[str, str]:
        raise NotImplementedError

    def body_session_token(self) -> str:
        return ""


def build_authenticator(
    connection: ConnectionConfig, transport: Transport
) -> Authenticator:
    """Select the auth strategy for this connection's auth mode."""
    if connection.auth_mode == "oauth":
        return OAuthS2SAuth(connection, transport)
    return NativeLogonAuth(connection, transport)
