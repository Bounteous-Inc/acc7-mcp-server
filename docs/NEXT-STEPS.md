# Next: implement `query_schema` and `get_entity`

These are the only unimplemented tools left (see `TOOLS.md` §2 for their target
contracts). Together they cover the ~700 configuration objects the migration audit has
to account for — folders (131), forms (193), options (188), JS libraries (131),
operators (40), enums (35), typology rules (21), external accounts (18), deliveries
(32), workflows (38).

They are complementary, not overlapping:

| | Reads | Reaches |
|---|---|---|
| `query_schema` | SQL columns, many rows | every schema; 234 of `nms:delivery`'s 581 fields |
| `get_entity` | one whole entity document **including its memo blob** | `@name`-keyed schemas only — ~630 of the ~700 objects |

`get_entity` is the only way to reach memo-mapped content (the other 347 fields on a
delivery). Its mechanism is already proven: `xtk:persist#GetEntityIfMoreRecent`
returned real content for 8/8 config schemas tried, including 3,114 chars of JS source
out of a memo (see `DESIGN.md` §7).

Current stubs: `Acc7Client.query`, `Acc7Client.get_entity`, all three functions in
`acc/v7/pagination.py`, and the tool bodies in `tools/query.py` and `tools/entity.py`.

---

## Part 1 — shared: error enrichment

Both tools fail the same two ways, and ACC's fault does not distinguish them:

> *"Attribute 'useDefaultErrorAddress' unknown (see definition of schema 'Deliveries (nms:delivery)')"*

That reads identically whether the caller typo'd a field or asked for a field that
genuinely exists but is memo-mapped and unselectable. Those need opposite corrections,
and both are likely agent mistakes.

Add a helper (new `acc/v7/diagnose.py`, or alongside `errors.py`) that, on a fault
matching `Attribute '(\w+)' unknown` / `Element '(\w+)' unknown`, fetches
`get_schema_inventory()` and re-raises with:

- name matches a field flagged `"xml": true` → *"`content` is stored in a memo column
  and cannot be selected with query_schema. Use get_entity for the full document."*
- otherwise → close matches via `difflib.get_close_matches`.

Costs one extra call **only on the error path**. Build it first; both tools use it.

---

## Part 2 — `query_schema`

### Paging: keyset by default, because offset is measurably wrong

Measured on `xtk:workflowTask` (40,522 rows): `startLine=0` → ids
`[1000,1030,1060,1090,1120]`, `startLine=5` → `[1120,1150,…]`. **Id 1120 appears in
both pages** — ACC's `startLine` is off by one, duplicating a row at every boundary.
Keyset (`@id > <last>` ordered by `@id`) paged cleanly over three pages with no
overlap.

Offset is used only as a fallback, in two cases:
- **Schema has no `@id`** — real: `nl:monitoring`, `xtk:sessionInfo` fail with
  *"Attribute 'id' unknown"*.
- **Caller supplies `order_by`** — their ordering wins, so `@id` keyset no longer
  applies.

Both cases set `paging: "offset"` in the response with a note about the duplicate
risk. Silently returning duplicates is the failure to avoid.

`@id` is auto-added to the select in keyset mode (the cursor is built from it); the
response says so.

### Files

**`acc/v7/pagination.py`** — fill the three stubs. `Page` already exists.
- `encode_cursor` → opaque base64 JSON, including a short hash of
  `(schema, fields, where, order_by)` so `decode_cursor` can reject a cursor pasted
  from a *different* query instead of returning wrong rows.
- `decode_cursor` → `Page`, raising `QueryError` with actionable text on
  malformed/mismatched input.
- `supports_keyset` → not a static answer. Make it a per-client cache populated by
  observation: try keyset, and on *"Attribute 'id' unknown"* mark the schema
  offset-only and retry. Cheaper and more accurate than a schema fetch per query.

**`acc/v7/client.py`** — implement `query()`: validate via existing
`_validate_schema_name`; decode cursor; build with `build_execute_query(...)` (already
handles `where`/`order_by`/`line_count`/`start_line`); call through `self._call(...)`
so a re-logon rebuilds the envelope with a fresh token; parse with `parse_rows()`.
Return `(rows, next_cursor)`; `next_cursor` is `None` when fewer rows return than
requested. Page size `min(requested or acc_default_page_size, acc_max_rows)`.

**`tools/query.py`** — follow the shape in `tools/discovery.py`: `get_client()` →
call → `cap_rows(...)` → `tool_result.ok`, `except Exception` →
`tool_result.error(exc, context={"schema": schema})`. Response carries `schema`,
`rows`, `returned`, `truncated`, `next_cursor`, `paging`, and `note` where relevant.
The docstring is the agent-facing contract: SQL columns only, memo fields need
`get_entity`, `count_records` when only a total is wanted.

---

## Part 3 — `get_entity`

Smaller: the envelope (`build_get_entity`), the parser (`parse_entity_document`) and
`cap_document` all exist and are in use by `get_schema_definition`.

### Key formats are not uniform — the main thing to get right

Verified across config schemas:

| Schema | Working key |
|---|---|
| `xtk:javascript`, `xtk:form` | `xtk:javascript\|nms:aaexception.js` — **namespace-qualified** |
| `xtk:folder`, `xtk:option`, `nms:typology`, `nms:typologyRule`, `nms:extAccount`, `nms:deliveryMapping`, `xtk:operator`, `xtk:enum` | `xtk:folder\|aggregates` — **bare name** |

Rule: schemas carrying a `namespace` field want `namespace:name`; the rest take the
bare name. Rather than make the agent guess, the tool should on a not-found error
consult the inventory and, if the schema has a `namespace` field, say so explicitly
with the corrected form.

### Files

**`acc/v7/client.py`** — implement `get_entity()`: validate the `schema|value` shape
(reuse `_SCHEMA_RE` for the left side, require a non-empty right side);
`self._call('xtk:persist#GetEntityIfMoreRecent', lambda t: build_get_entity(t, key, must_exist=must_exist))`;
return `parse_entity_document(body)`.

**`tools/entity.py`** — `cap_document(xml, acc_max_response_chars, entity_key=key)` →
`tool_result.ok`.

### Behaviour that must be explicit, not silent

`nms:delivery` and `xtk:workflow` **cannot** be fetched — the key resolver always
builds `where @name = …` and neither has `@name`. `EntityKeyError` already exists with
the right message ("use query_schema instead"); confirm it fires and that the tool
docstring states the limitation up front, so agents do not burn calls discovering it.

Caveat to note in the docstring: name-keyed lookup assumes the name is unique within
the schema. Where it is not (folders are the likely case), use `query_schema` to
disambiguate first.

Deferred: `strMd5` is accepted by `build_get_entity` and always passed empty. Paired
with the `md5` that `list_schemas` already returns free, that is the Phase 2 caching
hook — out of scope here.

---

## Order

1. Error-enrichment helper — shared, small.
2. `query_schema` — most work (pagination from scratch).
3. `get_entity` — reuses everything above; roughly 20 lines plus key-format guidance.

## Verification

Against the live instance over stdio.

**`query_schema`**
1. Config listings — `xtk:folder`, `xtk:option`, `nms:delivery`; totals agree with
   `count_records` (deliveries 32, workflows 38).
2. Keyset paging — three pages of `xtk:workflowTask` at `page_size=5`; assert **no id
   appears twice**, the exact thing offset paging fails.
3. Offset fallback — `nl:monitoring` (no `@id`) succeeds, reports `paging: "offset"`,
   carries the note.
4. Cursor integrity — a cursor from one query rejected by another.
5. Error enrichment — `fields: ["content"]` on `nms:delivery` names the memo
   constraint and points at `get_entity`; `@internalNam` suggests `@internalName`.
6. Caps — `page_size=10000` clamped to `acc_max_rows`, `truncated` set.

**`get_entity`**
7. Both key styles — `xtk:folder|aggregates` and `xtk:javascript|nms:aaexception.js`;
   the latter must return the JS source, proving memo content arrives.
8. Bare name against a namespace-qualified schema → error naming the qualified form.
9. `nms:delivery|globalDeliveryRcp` → `EntityKeyError` pointing at `query_schema`, not
   a raw fault.
10. A large entity truncates with `truncated: true` and the "will not parse" note.

**Regression** — `test_connection`, `list_schemas` 207/207, `count_records`
207/32/38, `get_schema_definition` inventories untrimmed.

## Notes

- `OAuthS2SAuth` stubs stay unimplemented — deliberate; needs `nlserver config
  -setimsoauth` on the instance.
- After these, the only open item is memo content for the ~70 deliveries and
  workflows, which needs the console packet capture rather than more code
  (`DESIGN.md` §6).
