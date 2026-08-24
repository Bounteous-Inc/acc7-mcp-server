"""HTTP transport: one httpx client, bounded retries, token-safe logging."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from ..config import ServerSettings

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0

# ACC error codes, e.g. XSV-350012, XTK-170036, SOP-330011.
_ACC_ERROR_CODE = re.compile(r"\b[A-Z]{2,4}-\d{6}\b")


class TransportFailure(Exception):
    """Network failure, or retries exhausted. Wrapped by the client layer."""


class Transport:
    """Owns the httpx client and the retry policy.

    Retries with exponential backoff on connection errors, timeouts, 5xx and 429
    (honouring Retry-After). Never retries auth failures or malformed queries.

    Note: ACC returns SOAP faults with a variety of status codes — a failed
    Logon comes back as HTTP 403 with the fault in the body. Non-2xx responses
    are therefore returned to the caller for fault parsing rather than raised
    on, unless the body carries no fault at all.
    """

    def __init__(self, settings: ServerSettings) -> None:
        self._settings = settings
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.acc_timeout_seconds,
                verify=self._settings.acc_verify_tls,
                follow_redirects=False,
            )
        return self._client

    async def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), _BACKOFF_CAP_SECONDS))
                return
            except ValueError:
                pass
        await asyncio.sleep(min(_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS))

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        client = self._get_client()
        attempts = max(1, self._settings.acc_max_retries)
        last_error: str = "unknown"

        for attempt in range(attempts):
            try:
                response = await client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    await self._sleep_backoff(attempt)
                    continue
                break

            # Transient server-side conditions are worth another go.
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                if attempt < attempts - 1:
                    await self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                    continue
            return response

        raise TransportFailure(
            f"{method} {url} failed after {attempts} attempt(s): {last_error}"
        )

    async def post_soap(
        self,
        url: str,
        soap_action: str,
        envelope: str,
        headers: dict[str, str] | None = None,
    ) -> str:
        """POST a SOAP envelope and return the raw response body."""
        merged = {
            "Content-Type": "text/xml; charset=UTF-8",
            "SOAPAction": soap_action,
            **(headers or {}),
        }
        response = await self._request(
            "POST", url, content=envelope.encode("utf-8"), headers=merged
        )
        body = response.text
        # A non-2xx carrying an ACC error is the instance telling us something
        # specific — hand it to the parser. ACC signals errors two ways: a
        # SOAP fault element, or a bare plain-text line like
        # "XSV-350012 Invalid login or password." (what a rejected Logon
        # returns, with HTTP 403 and no XML). Only a 4xx/5xx with neither is a
        # genuine transport-level problem.
        if (
            response.status_code >= 400
            and "Fault" not in body
            and not _ACC_ERROR_CODE.search(body)
        ):
            raise TransportFailure(
                f"HTTP {response.status_code} from {url} with no ACC error in body"
            )
        return body

    async def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """Authenticated HTTP GET, used for schemawsdl.jsp and /r/test."""
        response = await self._request("GET", url, params=params, headers=headers or {})
        if response.status_code >= 400:
            raise TransportFailure(f"HTTP {response.status_code} from {url}")
        if response.status_code in (301, 302, 303, 307, 308):
            raise TransportFailure(
                f"HTTP {response.status_code} redirect from {url} — "
                "the endpoint requires an authenticated session"
            )
        return response.text

    async def aclose(self) -> None:
        """Close the underlying client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class _TokenRedactionFilter(logging.Filter):
    """Scrubs known secrets out of every log record."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def add(self, *secrets: str) -> None:
        for secret in secrets:
            if secret and len(secret) >= 8:
                self._secrets.add(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_redaction_filter = _TokenRedactionFilter()
_redaction_installed = False


def install_token_redaction(session_token: str, security_token: str) -> None:
    """Register a logging filter that scrubs the given tokens from all records.

    Guards against an accidental `logger.debug(response)` leaking credentials.
    """
    global _redaction_installed
    _redaction_filter.add(session_token, security_token)
    if not _redaction_installed:
        logging.getLogger().addFilter(_redaction_filter)
        for name in ("httpx", "httpcore", "acc_mcp", "fastmcp"):
            logging.getLogger(name).addFilter(_redaction_filter)
        _redaction_installed = True


def redact(text: str) -> str:
    """Scrub known secrets from an arbitrary string, e.g. before raising."""
    for secret in _redaction_filter._secrets:
        text = text.replace(secret, "***REDACTED***")
    return re.sub(r"(__sessiontoken=)[^\s;]+", r"\1***REDACTED***", text)
