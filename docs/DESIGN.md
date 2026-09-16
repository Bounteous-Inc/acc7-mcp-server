# ACCv7 MCP Server — Design & Decisions

Why the server is built the way it is: architecture, the decisions behind it, where the
original design doc turned out to be wrong once checked against a live instance, and
what's still open. For tool inputs/outputs, see `TOOLS.md`.

**Last verified:** 2026-08-24 against `https://ac984us.adobesandbox.com` (v7.4.3, build 9396).

---

## 1. Context

We are migrating an Adobe Campaign Classic v7 instance to ACCv8. The analysis work
(inventory, dependency mapping, effort sizing) previously depended on manual XML/CSV
exports. This MCP server replaces that with live, read-only access to the v7 instance
so an AI agent can pull schemas, workflows and deliveries on demand and derive insights
itself.

Scope is **Phase 1 only**: read-only tools against v7. No writes, no v8 side, no
resources/caching layer, no insight generation inside the server.

### Key platform facts that shape the design

- **v7 is SOAP-only.** No REST API. Every interaction is a SOAP call to
  `soaprouter.jsp`, or an HTTP GET against a JSSP page (e.g. the WSDL generator).
- **Schemas are the core abstraction.** Workflows, deliveries, recipients, and custom
  tables are all "schemas" — XML documents describing a database table.
- **Generic query API.** `xtk:queryDef#ExecuteQuery` reads records from *any* schema
  with a uniform request shape, so one code path handles all schemas.
- **Dynamic discovery makes it client-agnostic.** The server discovers schemas at
  runtime (querying `xtk:schema`), so the same server works against any client's
  instance regardless of their custom schemas.

---

## 2. Architecture

```
Claude (client) ─ MCP protocol (stdio / HTTP) ─▶ MCP Server
                                                    │
                                          MCP layer: tools
                                                    │
                                        AccAdapter protocol
                                     (list_schemas, query, count,
                                      get_entity, get_schema_definition, ping)
                                                    │
                                    Acc7Client (SOAP over HTTPS)
                                                    │
                                   ACCv7 Application Server (soaprouter.jsp + DB)
```

**MCP layer** — exposes tools; the contract with Claude.

**`AccAdapter` protocol** — the v8 seam. Tools depend only on this protocol, not on
`Acc7Client` directly. If a v8 (REST) adapter is added later, it slots in beside the v7
one without touching the MCP layer or any tool code.

**`Acc7Client`** — all v7-specific plumbing: authenticate, build SOAP envelopes, send
them, parse responses, handle faults, respect rate limits. This is where the SOAP mess
is contained.

**Transport** — SOAP calls go to `https://<server>/nl/jsp/soaprouter.jsp`. WSDL
generation is an authenticated HTTP GET to
`https://<server>/nl/jsp/schemawsdl.jsp?schema=<schema>`.

Layout on disk:

```
src/acc_mcp/
├── config.py          env → typed Settings
├── server.py          FastMCP app, tool registration, transport
├── formatting.py      row/char caps, truncation envelopes
├── tools/             one module per tool group
└── acc/
    ├── base.py        AccAdapter protocol — the v8 seam
    ├── transport.py   httpx client, retries, token redaction
    └── v7/            SOAP adapter: auth, envelopes, parser, pagination, errors
```

---

## 3. Decisions locked in

| Question | Decision |
|---|---|
| Language / framework | Python + FastMCP (standalone `fastmcp`) |
| Phase | Phase 1 only; Phase 2 (resources/caching) after Phase 1 is verified |
| Dependency mapping | In scope — drives the `get_entity` tool |
| Transport | Selected by `MCP_TRANSPORT`: `stdio` (single-tenant, env) or `http` (default; multi-tenant, `X-ACC-*` headers) |
| Docker | Not now — env-only config and a single entrypoint make adding one later trivial |
| Secrets | `.env` file, `.env.example` committed |
| Testing | No formal test suite in Phase 1 |
| Output format | JSON for tabular results, raw XML (in a thin JSON wrapper) for entity documents |
| PII guarding / allowlists | Deferred to a later phase |

### Why JSON for some things and raw XML for others

- **The SOAP envelope itself** (`Envelope`/`Body`/`xsi:type`/`encodingStyle`) is pure
  protocol scaffolding — stripping it is unwrapping, not transformation. Always done.
- **Tabular results** (`query_schema`, `count_records`) come back as flat rows of
  attributes, nothing hierarchical — JSON here is lossless and roughly halves token
  cost.
- **Entity documents** (schema definitions, workflow bodies) are genuinely
  hierarchical; element nesting/ordering carries meaning, so flattening to JSON would
  be lossy. These stay raw XML, wrapped in JSON metadata (entity key, char count,
  truncation flag).

### What "compiled" vs "source" schema means

ACC stores two representations of every schema:

- **`xtk:srcSchema`** — the source you edit in the console. For an extension it
  contains *only the added fields* plus `extendedSchema="nms:recipient"`. This is the
  single most valuable artifact for the audit: "what this client customised."
- **`xtk:schema`** — the compiled/generated schema, merging the base with every
  extension and resolving SQL names, indexes, keys and links: "what the table
  effectively looks like at runtime."

Both are needed, hence `get_schema_definition`'s `form` parameter.

### What dependency mapping means here

Building a reference graph across the instance. Every edge lives **inside an entity's
XML body**, not in a flat column — which is why `get_entity` exists:

| Edge | Where it lives |
|---|---|
| workflow → schema | `<queryDef schema="cus:x">` inside a query activity |
| workflow → delivery template | `deliveryTemplate` / `@delivery` on a delivery activity |
| workflow → JS code/library | `<script>` body inside a JS activity; `xtk:javascript` refs |
| workflow → workflow | `signal` / `jump` / `subWorkflow` activity targets |
| schema → schema | `<element type="link" target="nms:recipient">` |
| schema → base schema | `srcSchema/@extendedSchema` — extensions vs. new tables |
| delivery → typology/mapping/template | attrs + child elements on the delivery entity |

**Verified coverage:** schema→schema (joins) and schema→base-schema (extensions) work
via `get_schema_definition`. Workflow→* (the activity graph) is blocked — see
[Open questions](#6-open-questions).

---

## 4. Authentication

Two supported paths, in preference order:

**A — Native technical operator (used).** A dedicated operator with a native login and
password, scoped to read rights. The server calls `xtk:session#Logon` and caches the
returned tokens. This is the path Phase 1 targets and what the live sandbox accepts.

**B — OAuth Server-to-Server (not implemented).** No `Logon` call; each request would
carry `Authorization: Bearer <IMS token>`. Setup needs an Adobe Developer Console
project plus `nlserver config -setimsoauth:...` on the instance host — shell access to
the Campaign container, which on a hosted sandbox is an Adobe support request, not a
self-service step.

**C — Login+password on every call.** Legacy mode requiring relaxed security-zone
settings. Not supported, and not configurable on a hosted instance anyway.

Auth sits behind an `Authenticator` interface selected by `ACC_AUTH_MODE`.
`NativeLogonAuth` is implemented; `OAuthS2SAuth` is stubbed and deliberately
unimplemented until an instance is actually OAuth-configured. Neither choice touches
the tool layer. Runtime details (token headers, storage, locking) are in `TOOLS.md` §1.

---

## 5. Deviations from the original design doc

The project started from a design doc written before any live-instance access. Probing
`https://ac984us.adobesandbox.com` turned up several corrections, folded into the build
from the start:

1. **Python + FastMCP** instead of the originally-sketched Node/TypeScript.
2. **Both tokens are mandatory on every call**, not just writes — the original doc
   omitted the `__sessiontoken` cookie requirement.
3. **Auth made pluggable** via an `Authenticator` interface (native now, IMS stubbed).
4. **`get_schema_definition` re-based off `GetEntityIfMoreRecent`, not WSDL.**
   `schemawsdl.jsp` emits SOAP method signatures, not fields/types/links/indexes/
   `extendedSchema` — useless for a structural audit. It's now the `wsdl` form, not
   the default; `inventory` (derived from compiled) is.
5. **`GetEntityIfMoreRecent` lives on `xtk:persist`, not `xtk:session`,** with params
   `strPk`/`strMd5`/`bMustExist` — not `pk`/`md5`/`mustExist`. Adobe's published API
   reference is misleading here; calling it under `urn:xtk:session` returns
   `SOP-330024 Unspecified function library`. The instance's own generated WSDL is
   authoritative over Adobe's docs.
6. **`schemawsdl.jsp` requires a session** — 302 without one, 200 with. The original
   doc's sample code fetched it unauthenticated.
7. **`@extendedSchema` doesn't exist on `xtk:schema`** (`XTK-170036`) — only on
   `xtk:srcSchema`. Dropped from `list_schemas`; extension detection happens via
   `get_schema_definition(form="source")` instead.
8. **`@scheduling` is not an attribute on `nms:delivery`** — it's a child element.
9. **Pagination changed to keyset-by-`@id`**, offset as fallback, always with an
   injected `orderBy`. Measured on `xtk:workflowTask` (40,522 rows): `startLine=0` and
   `startLine=5` both return id `1120` — ACC's offset paging is off-by-one and
   duplicates rows at page boundaries. The original doc's "`SelectAll` + session-held
   cursor" idea is also wrong: `SelectAll` selects all *columns*, not a cursor.
10. **Added `get_entity`, `count_records`, `test_connection`** — none were in the
    original three-tool design; dependency mapping, sizing, and stage-instance
    diagnostics all need them.
11. **Hard output caps** (500 rows / 120,000 chars, raised from an initial 50k once
    real schema sizes were measured) with explicit truncation markers, so the agent
    knows when it saw a partial result.
12. **`get_schema_definition` defaults to `inventory`, not raw XML.** Raw compiled XML
    is 236 KB for `nms:delivery` and 535 KB for `xtk:workflow` — the response cap
    truncated them to 21% and 9%, mid-element, unparseable. The inventory form lists
    every field (memo-mapped ones flagged, not dropped) and every enumeration, so it's
    complete for audit purposes at a fraction of the size (median ~4.7 KB across 24
    schemas sampled; the two above are ~50x outliers, not the norm).
13. **Phase 1 ships six tools, not three.**

---

## 6. Open questions

### Entity memo content — deliveries and workflow activities (unresolved)

A timeboxed spike (2026-08-24) established this is an **addressing problem, not a
memo problem**:

- ✅ Memo content retrieves fine for `@name`-keyed schemas — `GetEntityIfMoreRecent` /
  `LoadAsText` on `xtk:javascript|nms:aaexception.js` returned 3,114 chars of real JS
  source.
- ❌ `nms:delivery` and `xtk:workflow` have no `@name` (they use `@internalName`), and
  the entity-key resolver always builds `where @name = ...`. Every pk form fails.
- ❌ `queryDef` can't reach memo content either way: container selects return empty
  elements, leaf selects error `"Element 'var' unknown"` despite being declared in the
  compiled schema, omitting `<select>` returns a bare element, `fullLoad="true"`
  changes nothing.
- ❌ `Load`/`LoadIfExists`/`LoadAsText` fall back to reading
  `datakit/eng/<type>/<id>.xml` off the server's disk for non-`@name` schemas —
  useless for a hosted instance.
- ❌ `nms:delivery#LoadExpandedContents` → `SOP-330011`, empty detail.
- ❌ `xtk:queryDef#GetXmlStruct` → echoes the queryDef back, not entity content.

**This is the largest gap in the audit** — it blocks delivery `content`/`targets`/
`mailParameters`/`tracking`/`scheduling` and all workflow activity XML, which is the
substance the testing team verifies.

**Untried, in order of promise:** capture what the client console sends when opening a
delivery (it clearly can load them, so a working call exists); check whether a pk form
derived from the schema's declared `<key>` is accepted; investigate JSSP endpoints
outside `soaprouter.jsp`.

**Partial fallback:** `xtk:workflowTask` holds 40,522 rows on this instance with an
`@activity` attribute and a link to `xtk:workflow` — but these are *execution*
records, showing which activities ran, not the workflow's definition.

### Other open items

- **API account rights.** `mcp_api` can currently read `xtk:schema`, `xtk:srcSchema`,
  `xtk:workflow`, `nms:recipient` — enough for a stage sandbox. Before pointing at
  production: request a dedicated technical operator (not a shared human login) scoped
  to read-only on the folders/schemas actually in scope.
- **MCP-layer auth.** Under `http`, the endpoint is currently unauthenticated —
  anything reaching it inherits the service account's read access. Bound to
  `127.0.0.1` this is fine for a local sandbox POC. Before binding to anything else:
  add a bearer token on the transport, OAuth 2.1 (FastMCP has helpers), or
  network-level restriction.
- **Session TTL** — 24h default, configurable in `serverConf.xml`; transparent
  re-logon covers it regardless.
- **PII posture** — deferred. Until addressed, the server can read recipient data if
  asked; keep it pointed at stage, not production.

---

## 7. Verification log

Every call below was executed directly against the live instance before/alongside the
corresponding tool being built, so the build reproduces known-good calls rather than
guessing at a contract.

| Check | Result |
|---|---|
| `GET /r/test` | `status='OK' version='7.4.3' build='9396' instance='partners'` |
| `xtk:session#Logon` as `mcp_api` | HTTP 200, session + security tokens returned |
| `ExecuteQuery` on `xtk:schema` | rows returned (after dropping `@extendedSchema`) |
| `ExecuteQuery operation="count"` on `nms:recipient` | `count="1"` |
| `ExecuteQuery` on `xtk:workflow` | real rows — `cleanup`, `tracking`, `billing`, … |
| `schemawsdl.jsp` without / with session | 302 → 200 (65 KB) |
| `xtk:persist#GetEntityIfMoreRecent` on `xtk:srcSchema\|nms:recipient` | 11.7 KB source schema |
| same on `xtk:schema\|nms:recipient` | 18.6 KB compiled, 22 joins, 13 keys, 12 indexes |
| same on `xtk:workflow\|<id>` | fails — see [Open questions](#6-open-questions) |
| `xtk:persist#GetEntityIfMoreRecent`/`LoadAsText` on `xtk:javascript\|nms:aaexception.js` | 3,114 chars of real JS source |

Once `query_schema`/`get_entity` land, re-run against the live instance per the
verification checklist in `NEXT-STEPS.md`.
