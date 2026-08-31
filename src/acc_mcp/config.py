"""Configuration, split into two layers.

`ServerSettings` is operational config for this process — timeouts, caps,
retries, bind address. It comes from the environment and applies to everyone.

`ConnectionConfig` is which Campaign instance to talk to and as whom. Where it
comes from depends on the transport, because the two have different channels
available:

    stdio  single-tenant, one process per client -> environment variables
    http   multi-tenant, many clients per process -> X-ACC-* request headers

Either way the credentials never pass through tool arguments, so they never
enter the model's context.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Headers a client sends to say which instance it wants.
HEADER_BASE_URL = "x-acc-base-url"
HEADER_LOGIN = "x-acc-login"
HEADER_PASSWORD = "x-acc-password"
HEADER_AUTH_MODE = "x-acc-auth-mode"

_HTTP_HELP = (
    "Connection details are supplied per client, as HTTP headers:\n"
    "  X-ACC-Base-Url:  https://<instance-host>   (scheme + host only)\n"
    "  X-ACC-Login:     <native operator login>\n"
    "  X-ACC-Password:  <operator password>\n"
    "  X-ACC-Auth-Mode: native   (optional, default)\n\n"
    "With Claude Code:\n"
    '  claude mcp add --transport http acc7 http://127.0.0.1:8000/mcp \\\n'
    '    --header "X-ACC-Base-Url: https://<host>" \\\n'
    '    --header "X-ACC-Login: <login>" \\\n'
    '    --header "X-ACC-Password: <password>"'
)

_STDIO_HELP = (
    "Under stdio the server has no HTTP headers to read, so connection "
    "details come from environment variables:\n"
    "  ACC_BASE_URL=https://<instance-host>   (scheme + host only)\n"
    "  ACC_LOGIN=<native operator login>\n"
    "  ACC_PASSWORD=<operator password>\n"
    "  ACC_AUTH_MODE=native   (optional, default)\n\n"
    "With Claude Code:\n"
    "  claude mcp add acc7 \\\n"
    "    -e ACC_BASE_URL=https://<host> \\\n"
    "    -e ACC_LOGIN=<login> \\\n"
    "    -e ACC_PASSWORD=<password> \\\n"
    "    -- /abs/path/to/.venv/bin/acc-mcp-server"
)


def connection_help(transport: str) -> str:
    """Guidance for supplying connection details on the active transport.

    Transport-specific on purpose: telling a stdio user to send HTTP headers
    would be advice they cannot act on.
    """
    return _STDIO_HELP if transport == "stdio" else _HTTP_HELP


# Field labels differ by transport too, so errors name the thing the user
# actually sets.
_FIELD_LABELS = {
    "stdio": ("ACC_BASE_URL", "ACC_LOGIN", "ACC_PASSWORD", "ACC_AUTH_MODE"),
    "http": ("X-ACC-Base-Url", "X-ACC-Login", "X-ACC-Password", "X-ACC-Auth-Mode"),
}


class ConfigurationError(Exception):
    """A required setting is missing or unusable."""


class ServerSettings(BaseSettings):
    """Process-wide operational settings. Not instance-specific."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Behaviour ---
    acc_timeout_seconds: int = 60
    acc_default_page_size: int = 200
    acc_max_rows: int = 500
    acc_max_response_chars: int = 120_000
    acc_max_retries: int = 3
    acc_verify_tls: bool = True
    acc_log_level: str = "INFO"
    acc_max_cached_connections: int = 32

    # --- Transport ---
    # stdio: Claude launches one process per client; connection details come
    #        from env, and the process is single-tenant.
    # http:  one process serves many clients; connection details come from
    #        per-request X-ACC-* headers.
    mcp_transport: Literal["stdio", "http"] = "http"
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8000

    @field_validator("mcp_transport", mode="before")
    @classmethod
    def _normalise_mcp_transport(cls, value: object) -> object:
        """Accept casing variants, and treat 'https' as 'http'.

        The process always serves plain HTTP; TLS is terminated by whatever
        proxy sits in front of it. Rejecting 'https' would be pedantry.
        """
        if isinstance(value, str):
            cleaned = value.strip().lower()
            return "http" if cleaned in ("http", "https") else cleaned
        return value

    # --- Local development fallback ---
    # Off by default. A deployed server must never silently serve someone
    # else's instance because a client forgot its headers, so the fallback has
    # to be switched on deliberately.
    acc_allow_env_fallback: bool = False
    acc_base_url: str = ""
    acc_auth_mode: Literal["native", "oauth"] = "native"
    acc_login: str = ""
    acc_password: str = ""


@dataclass(frozen=True)
class ConnectionConfig:
    """Which instance to talk to, and as whom. Hashable — used as a cache key."""

    base_url: str
    login: str
    password: str
    auth_mode: Literal["native", "oauth"] = "native"

    @property
    def _base(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def soap_url(self) -> str:
        """Full URL of the SOAP router endpoint."""
        return f"{self._base}/nl/jsp/soaprouter.jsp"

    @property
    def wsdl_url(self) -> str:
        """Full URL of the WSDL generator JSSP page."""
        return f"{self._base}/nl/jsp/schemawsdl.jsp"

    @property
    def test_url(self) -> str:
        """Unauthenticated health endpoint. Reports version, build, instance."""
        return f"{self._base}/r/test"

    def describe(self) -> dict[str, str]:
        """Non-sensitive identity, safe to echo back in results and errors."""
        return {"instance_url": self.base_url, "login": self.login, "auth_mode": self.auth_mode}

    def validate(self) -> None:
        """Raise a clear error naming whatever is missing or unsupported.

        Messages name the thing the user actually sets on this transport — env
        vars under stdio, headers under http.
        """
        transport = get_server_settings().mcp_transport
        url_label, login_label, password_label, mode_label = _FIELD_LABELS[transport]

        missing = [
            label
            for label, value in (
                (url_label, self.base_url),
                (login_label, self.login),
                (password_label, self.password),
            )
            if not value.strip()
        ]
        if missing:
            raise ConfigurationError(
                f"Missing connection detail(s): {', '.join(missing)}.\n\n"
                f"{connection_help(transport)}"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigurationError(
                f"{url_label} must start with http:// or https:// "
                f"(got {self.base_url!r})."
            )
        if self.auth_mode != "native":
            raise ConfigurationError(
                f"Auth mode {self.auth_mode!r} is not implemented. This build "
                f"supports native operator logon; set {mode_label} to 'native' "
                "or leave it unset."
            )


@lru_cache(maxsize=1)
def get_server_settings() -> ServerSettings:
    """Return the process-wide operational settings."""
    return ServerSettings()


def resolve_connection() -> ConnectionConfig:
    """Build the ConnectionConfig for the current request.

    Under **stdio** there are no HTTP headers, so the environment is the only
    channel — read it unconditionally. That is not a weakening of the
    multi-tenant rule: Claude launches one process per client and passes each
    its own env (`claude mcp add acc7 -e ACC_BASE_URL=... -- cmd`), so a stdio
    process is single-tenant by construction.

    Under **http** one process serves many clients, so headers are required and
    the environment is consulted only when `ACC_ALLOW_ENV_FALLBACK=true` is set
    deliberately. That keeps a shared deployment from serving a stale instance
    to a client that forgot its headers.
    """
    from fastmcp.server.dependencies import get_http_headers

    settings = get_server_settings()
    headers = get_http_headers()  # never raises; empty when no HTTP request

    use_env = settings.mcp_transport == "stdio" or settings.acc_allow_env_fallback
    if use_env:
        defaults = (
            settings.acc_base_url,
            settings.acc_login,
            settings.acc_password,
            settings.acc_auth_mode,
        )
    else:
        defaults = ("", "", "", "native")

    connection = ConnectionConfig(
        base_url=headers.get(HEADER_BASE_URL, defaults[0]).strip(),
        login=headers.get(HEADER_LOGIN, defaults[1]).strip(),
        password=headers.get(HEADER_PASSWORD, defaults[2]),
        auth_mode=headers.get(HEADER_AUTH_MODE, defaults[3]).strip() or "native",  # type: ignore[arg-type]
    )
    connection.validate()
    return connection
