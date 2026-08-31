"""Response parsing: unwrap the SOAP envelope, detect faults, extract payloads.

The envelope (Envelope/Body/*Response/pdomOutput, xsi:type, encodingStyle) is
protocol scaffolding — stripping it loses nothing. What is inside is kept as
rows (tabular) or raw XML (documents).
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree

from .errors import AccError, classify_fault

# e.g. "XSV-350012 Invalid login or password. Connection denied."
_PLAIN_ERROR = re.compile(r"^[A-Z]{2,4}-\d{6}\b")

# ACC responses are not always strictly well-formed; recover so a stray
# entity does not cost us an otherwise readable fault message.
_PARSER = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)


def _parse(body: str) -> etree._Element:
    root = etree.fromstring(body.encode("utf-8"), parser=_PARSER)
    if root is None:
        raise AccError("Unparseable response from the Campaign instance")
    return root


def _find_local(root: etree._Element, local_name: str) -> etree._Element | None:
    """First element with this local name, namespace-agnostic."""
    for element in root.iter():
        if isinstance(element.tag, str) and etree.QName(element).localname == local_name:
            return element
    return None


def _text_of(root: etree._Element, local_name: str) -> str | None:
    element = _find_local(root, local_name)
    return (element.text or "") if element is not None else None


def raise_for_fault(body: str) -> None:
    """Raise the appropriate AccError if the body reports an error.

    Called on every response before any other parsing. Two shapes occur:

    * a `SOAP-ENV:Fault` element, which arrives with HTTP 200 — so a 200 alone
      never means success;
    * a bare plain-text error line such as
      `XSV-350012 Invalid login or password. Connection denied.`, which is what
      a rejected Logon returns (with HTTP 403 and no XML at all).
    """
    stripped = body.strip()

    # Plain-text error body — no envelope, no XML.
    if not stripped.startswith("<"):
        match = _PLAIN_ERROR.match(stripped)
        if match:
            raise classify_fault(stripped, "")
        return

    if "Fault" not in body:
        return
    root = _parse(body)
    fault = _find_local(root, "Fault")
    if fault is None:
        return
    fault_string = _text_of(fault, "faultstring") or ""
    detail = _text_of(fault, "detail") or ""
    raise classify_fault(fault_string, detail)


def parse_logon(body: str) -> tuple[str, str]:
    """Extract `(session_token, security_token)` from a Logon response."""
    root = _parse(body)
    session_token = _text_of(root, "pstrSessionToken")
    security_token = _text_of(root, "pstrSecurityToken")
    if not session_token:
        raise AccError("Logon succeeded but no session token was returned")
    return session_token, security_token or ""


def parse_session_info(body: str) -> dict[str, Any]:
    """Pull non-sensitive operator/instance details out of a Logon response.

    Only attribute data from <sessionInfo> — never the tokens.
    """
    root = _parse(body)
    info_element = _find_local(root, "sessionInfo")
    if info_element is None:
        return {}
    details: dict[str, Any] = {}
    for element in info_element.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element).localname
        for key, value in element.attrib.items():
            if key in ("login", "loginId", "loginCS", "timezone", "instanceName"):
                details[f"{name}.{key}" if name != "sessionInfo" else key] = value
    return details


def parse_rows(body: str) -> list[dict[str, Any]]:
    """Flatten an ExecuteQuery collection into row dicts.

    The payload sits under `pdomOutput` as a `<x-collection>` wrapping one
    element per row, e.g. `<schema-collection><schema name=... /></...>`.
    An empty result is an empty collection, not an error.

    ACC returns attributes beyond those selected (`label`, `img`, `md5`, `_cs`,
    `_isMemoNull`); they are preserved rather than filtered — `md5` in
    particular is a free change-detection key.
    """
    root = _parse(body)
    output = _find_local(root, "pdomOutput")
    if output is None:
        return []

    children = [child for child in output if isinstance(child.tag, str)]
    if not children:
        return []

    # One `<x-collection>` wrapper is the normal shape; fall back to treating
    # pdomOutput's children as the rows themselves.
    first = children[0]
    if len(children) == 1 and etree.QName(first).localname.endswith("-collection"):
        rows = [row for row in first if isinstance(row.tag, str)]
    else:
        rows = children

    return [dict(row.attrib) for row in rows]


def parse_count(body: str) -> int:
    """Extract the row count from an `operation="count"` response.

    A count reply is a single element carrying a `count` attribute, with no
    `-collection` wrapper and the element named after the schema's entity:

        <pdomOutput><delivery count="31"/></pdomOutput>
    """
    root = _parse(body)
    output = _find_local(root, "pdomOutput")
    if output is None:
        raise AccError("Count query returned no output document")

    for child in output.iter():
        if isinstance(child.tag, str) and "count" in child.attrib:
            raw = child.attrib["count"]
            try:
                return int(raw)
            except ValueError as exc:
                raise AccError(f"Count query returned a non-numeric count {raw!r}") from exc

    raise AccError("Count query returned no count attribute")


def parse_entity_document(body: str) -> str:
    """Return the entity XML from a GetEntityIfMoreRecent response, unwrapped.

    The payload sits under `pdomDoc` as a single element — the schema, source
    schema or other entity. Only the SOAP scaffolding is stripped; the document
    itself is returned verbatim, because element nesting and ordering carry
    meaning that a JSON projection would lose.

    Returns "" when the entity is absent and `bMustExist` was false.
    """
    root = _parse(body)
    holder = _find_local(root, "pdomDoc") or _find_local(root, "pdomOutput")
    if holder is None:
        raise AccError("Entity request returned no document")

    children = [child for child in holder if isinstance(child.tag, str)]
    if not children:
        return ""
    return etree.tostring(children[0], encoding="unicode")


def parse_instance_test(body: str) -> dict[str, Any]:
    """Parse the `<redir .../>` document served by /r/test."""
    root = _parse(body)
    return dict(root.attrib) if root is not None else {}
