"""Compact structural inventory of a schema.

`get_schema_definition` can return the raw compiled XML, which is the faithful
answer but not always a usable one: `nms:delivery` is 236k and `xtk:workflow`
534k, and truncating XML mid-element yields something that will not parse.

This produces the same structural facts a migration audit needs — every field
with its type, links with their join conditions, keys, indexes and
enumerations — at a fraction of the size. The saving comes from dropping what
an audit does not use (`<method>` signatures, `help`/`desc` prose, which are
~45% of a large schema) and from flattening nested paths, not from re-encoding:
XML→JSON of the same content is actually ~8% *larger* than the XML.

Nothing structural is silently omitted. Memo-mapped fields are listed like any
other, flagged `"xml": true`, and anything dropped to fit the response budget
is named in `trimmed`.
"""

from __future__ import annotations

import json
from typing import Any

from lxml import etree

_PARSER = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)


def _ln(element: etree._Element) -> str:
    return etree.QName(element).localname


def _kids(element: etree._Element, name: str) -> list[etree._Element]:
    return [c for c in element if isinstance(c.tag, str) and _ln(c) == name]


def _field(element: etree._Element, path: str, in_memo: bool) -> dict[str, Any]:
    """One field entry. Empty values are omitted to keep the payload small."""
    entry: dict[str, Any] = {"name": path, "type": element.get("type", "string")}
    for key in ("length", "label", "enum", "sqlname", "default"):
        value = element.get(key)
        if value:
            entry[key] = int(value) if key == "length" and value.isdigit() else value
    if element.get("required") == "true":
        entry["required"] = True
    if in_memo:
        # Stored inside an XML document in a memo column rather than in its own
        # SQL column. Queryable via get_schema_definition, but NOT selectable
        # in query_schema and its value is not retrievable over SOAP on v7.
        entry["xml"] = True
    return entry


def _walk(
    element: etree._Element,
    prefix: str,
    fields: list[dict[str, Any]],
    memo_roots: dict[str, int],
    memo_name: str | None,
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    for child in element:
        if not isinstance(child.tag, str):
            continue
        kind, name = _ln(child), child.get("name")
        if not name:
            continue
        path = f"{prefix}{name}"

        if kind == "attribute":
            fields.append(_field(child, path, bool(memo_name)))
            if memo_name:
                memo_roots[memo_name] = memo_roots.get(memo_name, 0) + 1

        elif kind == "element" and child.get("type") == "memo":
            # A real column of type memo — the blob itself. xtk:workflow's
            # activity document lives in `data`. It is a field, not a container.
            fields.append(_field(child, path, bool(memo_name)))

        elif kind == "element" and child.get("type") != "link":
            in_memo = memo_name or (name if child.get("xml") == "true" else None)
            if in_memo:
                memo_roots.setdefault(in_memo, 0)
            _walk(child, f"{path}/", fields, memo_roots, in_memo, depth + 1)


def build_inventory(xml: str, schema: str) -> dict[str, Any]:
    """Reduce a compiled schema document to a structural inventory."""
    root = etree.fromstring(xml.encode("utf-8"), parser=_PARSER)
    if root is None:
        return {"schema": schema, "error": "unparseable schema document"}

    wanted = schema.split(":", 1)[-1]
    # A compiled schema can declare several elements sharing the entity's name
    # (self-links, nested helpers). Document order picks the wrong one —
    # xtk:workflow yielded an empty shell that way. Prefer the element carrying
    # sqltable, then the largest subtree.
    candidates = [
        e
        for e in root.iter()
        if isinstance(e.tag, str) and _ln(e) == "element" and e.get("name") == wanted
    ]
    if not candidates:
        return {"schema": schema, "error": f"no root element named {wanted!r}"}
    with_table = [e for e in candidates if e.get("sqltable")]
    entity = max(with_table or candidates, key=lambda e: len(list(e.iter())))

    fields: list[dict[str, Any]] = []
    memo_roots: dict[str, int] = {}
    _walk(entity, "", fields, memo_roots, None)

    links = []
    for element in entity.iter():
        if not (isinstance(element.tag, str) and _ln(element) == "element"):
            continue
        if element.get("type") != "link" or not element.get("target"):
            continue
        entry: dict[str, Any] = {"name": element.get("name"), "target": element.get("target")}
        joins = [
            {"src": j.get("xpath-src"), "dst": j.get("xpath-dst")}
            for j in _kids(element, "join")
        ]
        if joins:
            entry["joins"] = joins
        if element.get("label"):
            entry["label"] = element.get("label")
        links.append(entry)

    keys = [
        {
            "name": k.get("name"),
            "fields": [kf.get("xpath") for kf in _kids(k, "keyfield")],
            **({"internal": True} if k.get("internal") == "true" else {}),
        }
        for k in entity.iter()
        if isinstance(k.tag, str) and _ln(k) == "key"
    ]

    indexes = [
        {"name": i.get("name"), "fields": [kf.get("xpath") for kf in _kids(i, "keyfield")]}
        for i in entity.iter()
        if isinstance(i.tag, str) and _ln(i) == "dbindex"
    ]

    # Every enumeration the schema declares, referenced or not — a custom enum
    # nothing points at yet is still something the migration has to carry.
    enums: dict[str, Any] = {}
    for enum in root.iter():
        if not (isinstance(enum.tag, str) and _ln(enum) == "enumeration" and enum.get("name")):
            continue
        enums[enum.get("name")] = [
            {"value": v.get("value"), "name": v.get("name"), "label": v.get("label")}
            for v in _kids(enum, "value")
        ]

    xml_field_count = sum(1 for f in fields if f.get("xml"))
    inventory: dict[str, Any] = {
        "schema": schema,
        "label": entity.get("label") or root.get("label"),
        "sqltable": entity.get("sqltable"),
        "counts": {
            "fields": len(fields),
            "sql_fields": len(fields) - xml_field_count,
            "xml_fields": xml_field_count,
            "links": len(links),
            "keys": len(keys),
            "indexes": len(indexes),
            "enumerations": len(enums),
        },
        "fields": fields,
        "links": links,
        "keys": keys,
        "indexes": indexes,
        "enumerations": enums,
    }
    if root.get("extendedSchema"):
        inventory["extends"] = root.get("extendedSchema")
    if memo_roots:
        inventory["memo_structures"] = [
            {"name": n, "fields": c} for n, c in sorted(memo_roots.items())
        ]
        inventory["memo_note"] = (
            'Fields marked "xml": true are stored inside an XML document in a '
            "memo column, not in their own SQL column. They cannot be selected "
            "with query_schema, and their values are not retrievable over SOAP "
            "on v7."
        )
    return inventory


def trim_inventory(inventory: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Shrink an inventory to fit the response budget, saying what it dropped.

    Trims least-useful-first and stops as soon as it fits: index definitions,
    then field labels, then the tail of the field list. Anything removed is
    named in `trimmed`, so a trimmed inventory can never be mistaken for a
    complete one.
    """

    def size() -> int:
        return len(json.dumps(inventory, default=str))

    if size() <= max_chars:
        return inventory

    dropped: list[str] = []

    def finish() -> dict[str, Any]:
        inventory["trimmed"] = dropped
        inventory["trimmed_note"] = (
            "This inventory was reduced to fit the response budget. Dropped: "
            + "; ".join(dropped)
            + ". Raise ACC_MAX_RESPONSE_CHARS, or use form='compiled' for the "
            "raw XML."
        )
        return inventory

    if inventory.get("indexes"):
        inventory["indexes"] = []
        dropped.append("indexes")
        if size() <= max_chars:
            return finish()

    for field in inventory.get("fields", []):
        field.pop("label", None)
        field.pop("default", None)
    dropped.append("field labels and defaults")
    if size() <= max_chars:
        return finish()

    # Budget the field list against what it actually costs, not against the
    # whole payload — scaling the field count by the total-size ratio throws
    # away far more than necessary.
    fields = inventory.get("fields", [])
    if fields:
        overhead = size() - len(json.dumps(fields, default=str))
        budget = max_chars - overhead
        kept: list[dict[str, Any]] = []
        running = 2  # the enclosing "[]"
        for field in fields:
            cost = len(json.dumps(field, default=str)) + 1
            if running + cost > budget:
                break
            kept.append(field)
            running += cost
        if len(kept) < len(fields):
            inventory["fields"] = kept
            dropped.append(f"fields beyond the first {len(kept)} of {len(fields)}")

    return finish()
