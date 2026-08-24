"""Paging strategies.

Keyset on `@id` is the default: offset paging on a live table silently skips or
duplicates rows. An orderBy is always injected — omitting it is the most common
cause of bad pagination. Without `lineCount`, ACC caps results at 10,000 rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Page:
    """One page request, in whichever paging mode applies."""

    page_size: int
    last_id: int | None = None
    start_line: int | None = None


def encode_cursor(page: Page) -> str:
    """Serialise paging state into an opaque cursor for the agent."""
    raise NotImplementedError


def decode_cursor(cursor: str | None, page_size: int) -> Page:
    """Parse a cursor back into paging state, or start a fresh scan."""
    raise NotImplementedError


def supports_keyset(schema: str) -> bool:
    """Whether `@id` keyset paging applies, or offset paging is the fallback."""
    raise NotImplementedError
