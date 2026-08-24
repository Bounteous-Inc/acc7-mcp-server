"""Output shaping: row caps, character caps, truncation envelopes.

Every tool result passes through here so the agent never receives an unbounded
payload, and always knows when it saw a partial result.
"""

from __future__ import annotations

import json
from typing import Any


def cap_rows(
    rows: list[dict[str, Any]],
    max_rows: int,
    max_chars: int,
) -> dict[str, Any]:
    """Cap a tabular result and wrap it with truncation metadata.

    Returns `{rows, returned, truncated, note}`. The agent must be able to tell
    a complete result from a partial one — silently truncating reads as
    "that's everything" when it is not.
    """
    total_available = len(rows)
    capped = rows[:max_rows]
    truncated = total_available > max_rows

    # Character cap is a second, independent ceiling: a few very wide rows can
    # blow the budget long before max_rows does.
    while capped and len(json.dumps(capped, default=str)) > max_chars:
        capped = capped[: max(1, len(capped) * 3 // 4)]
        truncated = True
        if len(capped) == 1:
            break

    result: dict[str, Any] = {
        "rows": capped,
        "returned": len(capped),
        "truncated": truncated,
    }
    if truncated:
        result["note"] = (
            f"Truncated to {len(capped)} row(s). Narrow the request with a "
            "filter or fewer fields, or page through the results — do not "
            "assume this is the complete set."
        )
    return result


def cap_document(
    xml: str,
    max_chars: int,
    *,
    entity_key: str | None = None,
) -> dict[str, Any]:
    """Wrap a raw XML document with metadata, truncating if oversized.

    Returns `{xml, bytes, truncated, entity_key}`. XML is returned as a string,
    not converted to JSON — element nesting and ordering carry meaning.
    """
    raise NotImplementedError
