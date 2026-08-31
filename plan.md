# ACCv7 MCP Server — Implementation Plan

**Status:** Ready to build — 5 of 6 tools verified against the live stage instance.
**Source design doc:** `acc7-mcp-server-plan.md`
**Last verified:** August 6, 2026 against `https://ac984us.adobesandbox.com` (v7.4.3, build 9396)

---

## Context

We are migrating an Adobe Campaign Classic v7 instance to ACCv8. The analysis work (inventory, dependency mapping, effort sizing) currently depends on manual XML/CSV exports. This MCP server replaces that with live, read-only access to the v7 instance so an AI agent can pull schemas, workflows and deliveries on demand and derive insights itself.

Scope is **Phase 1 only**: read-only tools against v7. No writes, no v8 side, no resources/caching layer, no insight generation inside the server. A stage instance is being provisioned; the server will be built against the documented SOAP contract and verified once credentials and network access land.

---

## 1. Decisions locked in

| Question | Decision |
|---|---|
| Language / framework | Python + FastMCP (standalone `fastmcp`, currently 3.4.5) |
| Instance access | Live — `ac984us.adobesandbox.com`, native operator `mcp_api`, verified working |
| Phase | Phase 1 only; Phase 2 (resources/caching) after Phase 1 is verified |
| Dependency mapping | In scope — drives the added `get_entity` tool |
| Transport | Selected by `MCP_TRANSPORT`: `stdio` (single-tenant, connection from env) or `http` (default; multi-tenant, connection from `X-ACC-*` headers). HTTP-only was tried 2026-08-09 and relaxed to a default on 2026-08-10. |
| Docker | Not now |
| Secrets | `.env` file, `.env.example` committed |
| Testing | No formal test suite expected in Phase 1 |
| Source control | Git repo |
| Output format | JSON for tabular results, raw XML for entity documents |
| PII guarding / allowlists | Deferred to a later phase |

---

## 2. Answers to the open questions raised during review

### 2.1 What "dependency mapping" means here

Building a reference graph across the instance:

| Edge | Where it actually lives |
|---|---|
| workflow → schema | `<queryDef schema="cus:x">` inside a query activity's XML |
| workflow → delivery template | `deliveryTemplate` / `@delivery` attr on a delivery activity |
| workflow → JS code / library | `<script>` body inside a JS activity; `xtk:javascript` refs |
| workflow → workflow | `signal` / `jump` / `subWorkflow` activity targets |
| schema → schema | `<element type="link" target="nms:recipient">` |
| schema → base schema | `srcSchema/@extendedSchema` — how you spot extensions vs. new tables |
| delivery → typology / mapping / template | attrs + child elements on the delivery entity |

Every one of these edges lives **inside an entity's XML body**, not in a flat column. That is why the three tools in the design doc cannot produce it: `query_schema` only returns the flat attributes you name, and `get_schema_definition` via `schemawsdl.jsp` returns SOAP *method signatures*, not link or extension structure.

The fix is one extra tool, `get_entity` (§4.5), backed by `xtk:persist#GetEntityIfMoreRecent`. The agent parses the returned XML and builds the graph — graph construction stays outside the server.

**Verified coverage (2026-08-06):**

| Edge class | Status |
|---|---|
| schema → schema (links/joins) | ✅ **Working.** Compiled `xtk:schema\|nms:recipient` returns 18.6 KB with 55 attributes, 27 elements, **22 `<join>` elements**, 13 key fields, 12 indexes, 3 enumerations. |
| schema → base schema (extensions) | ✅ **Working.** `xtk:srcSchema\|nms:recipient` returns 11.7 KB of source with `extendedSchema` visible. |
| workflow → * (activity graph) | ⚠️ **Unresolved.** See §10 "Still open" #1. Workflow *metadata* queries work; the activity XML does not yet come back. |

### 2.2 What `count_records` is for

`operation="count"` returns a row count without transferring rows. Three uses:

- **Sizing the migration** — how many recipients, active workflows, rows in each custom table.
- **Spotting dead objects cheaply** — a `cus:` table with 0 rows is a table you don't migrate.
- **Pre/post-migration reconciliation.**

One cheap call instead of paginating a table to learn its size, and it never floods context.

### 2.3 What "compiled" schema means

ACC stores two representations of every schema:

- **`xtk:srcSchema`** — the source you edit in the console. For an extension it contains *only the added fields* plus `extendedSchema="nms:recipient"`. This is the single most valuable artifact for the audit: it is literally "what this client customised".
- **`xtk:schema`** — the compiled/generated schema. ACC merges the base schema with every extension and resolves SQL table/column names, indexes, keys and links. This is "what the table effectively looks like at runtime".

Both are needed, so `get_schema_definition` takes a `form` parameter (`source` / `compiled` / `wsdl`).

### 2.4 What Docker would be for

Two things: a pinned, reproducible runtime to hand to whoever hosts the HTTP deployment, and network placement — if the stage instance is IP-allowlisted, the container runs on a host inside the allowlisted range. Neither matters for stdio dev. Not building it now; config is env-only and there is a single entrypoint, so adding a Dockerfile later is trivial.

### 2.5 Why not just return raw SOAP XML

Partly agreed — the server does both, depending on payload:

- **The SOAP envelope itself** (`Envelope` / `Body` / `ExecuteQueryResponse` / `pdomOutput`, `xsi:type` attributes, `encodingStyle`) is pure protocol scaffolding. Stripping it isn't a transformation, it's unwrapping — zero information lost, meaningful token savings. Always done.
- **Tabular results** (`query_schema`, `count_records`) come back as a flat `<schema-collection><schema @name @namespace/></schema-collection>` — rows of attributes, nothing hierarchical. JSON objects here are lossless and roughly halve the token cost. → **JSON**.
- **Entity documents** (schema definitions, workflow bodies) are genuinely hierarchical, and element nesting/ordering carries meaning. Flattening them to JSON *is* lossy and would confuse the reader. → **raw XML string**, inside a thin JSON wrapper carrying metadata (entity key, byte count, truncation flag).

### 2.6 Authentication — verified against the live stage instance

> **Correction.** An earlier draft of this plan claimed the IMS requirement was a v8.5.1+ change that did not apply to v7. That is wrong for this instance. Adobe's docs place the IMS expectation from **v7.3.1** onward, and require API integrations on **v7.4.1+** to use an OAuth Server-to-Server technical account. Our stage instance is **7.4.3**.

**Instance facts, probed 2026-08-06 against `https://ac984us.adobesandbox.com`:**

| Fact | Value | How established |
|---|---|---|
| Version / build | `7.4.3` / `9396` | `GET /r/test` |
| Instance name | `partners` (Adobe-hosted sandbox) | `GET /r/test` |
| Network reachability | OK, no VPN or IP allowlisting in the way | `GET /r/test` returned 200 |
| Console login modes | native login+password **and** "Sign-in with an Adobe ID" (`/nms/imslogin.jssp`) | `GET /nl/jsp/logon.jsp` |
| Native `Logon` with an Adobe ID | rejected — `HTTP 403`, `XSV-350012 Invalid login or password` | `xtk:session#Logon` probe |

The `XSV-350012` response is informative: the instance **evaluated** a native logon and rejected the credentials. It did not report native auth as disabled. So native operator logon remains available — it needs an operator that actually has a native password, which an IMS identity does not.

**Two supported paths, in preference order:**

**A — Native technical operator (primary).** Create a dedicated operator in the console (Administration → Access management → Operators) with a native login and password, scoped to read rights. Credentials go in `.env`; the server calls `xtk:session#Logon` and caches the returned tokens. Requires admin rights on the sandbox. This is the path the build targets.

**B — OAuth Server-to-Server (secondary).** No `Logon` call; each request carries `Authorization: Bearer <IMS access token>` with `<sessiontoken></sessiontoken>` left empty. Cleaner at runtime, but setup needs (i) an Adobe Developer Console project with the Campaign API + I/O Management API and an OAuth Server-to-Server credential, requiring System Administrator on the backing IMS org, **and** (ii) a server-side instance config, `nlserver config -instance:partners -setimsoauth:<org-id>/<client-id>/<tech-account-id>/<client-secret>`, which needs shell access to the Campaign container. On a hosted sandbox that is an Adobe support request, not a self-service step.

**C — Login+password on every call.** A legacy ACC mode requiring relaxed security-zone settings. Not supported, and not configurable on a hosted instance anyway.

**Design response (unchanged in shape, re-prioritised).** Auth sits behind an `Authenticator` interface selected by `ACC_AUTH_MODE`. `NativeLogonAuth` is implemented for Phase 1 because it is what this instance will accept. `OAuthS2SAuth` is a small, well-understood second implementation — the calling contract is documented above — added if and when the instance is OAuth-configured. Neither choice touches the tool layer.

**Also confirmed:**
- Native-auth calls need **both** `X-Security-Token: <securityToken>` and `Cookie: __sessiontoken=<sessionToken>`. Bearer-auth calls use neither.
- Session and security tokens live 24h by default (configurable in `serverConf.xml`).
- `schemawsdl.jsp` is authenticated and returns 401 without a session — the design doc's sample code fetched it unauthenticated.

**References**
- [Adobe — Web service calls](https://github.com/AdobeDocs/campaign-classic.en/blob/main/help/configuration/using/web-service-calls.md)
- [Adobe — Create and configure your Adobe technical account for APIs](https://experienceleague.adobe.com/en/docs/campaign-classic/using/integrating-with-adobe-experience-cloud/oauth-technical-account)
- [Adobe — Migration of technical operators to Adobe Developer Console (step 8 shows the bearer-token SOAP call)](https://experienceleague.adobe.com/en/docs/campaign-classic/using/technotes/ims/ims-migration)
- [Adobe — Migrate to IMS (campaign-classic)](https://experienceleague.adobe.com/en/docs/campaign-classic/using/technotes/ims/ac-ims)
- [Adobe — `GetEntityIfMoreRecent` API reference](https://experienceleague.adobe.com/developer/campaign-api/api/sm-session-GetEntityIfMoreRecent.html)
- [Adobe Campaign JS SDK — xtk:persist](https://opensource.adobe.com/acc-js-sdk/xtkPersist.html)

---

## 3. Project structure

```
acc_mcp_server/
├── .env.example                  # to be filled in → .env (gitignored)
├── .gitignore
├── README.md
├── DEVIATIONS.md                 # §7 of this plan, tracked in-repo
├── pyproject.toml
└── src/acc_mcp/
    ├── config.py                 # env → typed Settings (pydantic-settings)
    ├── server.py                 # FastMCP instance, tool registration, transport selection
    ├── formatting.py             # row caps, char caps, truncation envelope
    ├── tools/
    │   ├── discovery.py          # list_schemas, get_schema_definition
    │   ├── query.py              # query_schema, count_records
    │   ├── entity.py             # get_entity
    │   └── diagnostics.py        # test_connection
    └── acc/
        ├── base.py               # AccAdapter protocol — the v8 seam
        ├── transport.py          # httpx client, retries, backoff, timeouts
        └── v7/
            ├── client.py         # Acc7Client (implements AccAdapter)
            ├── auth.py           # Authenticator, NativeLogonAuth, ImsAuth (stub)
            ├── envelopes.py      # SOAP envelope builders
            ├── parser.py         # unwrap envelope, detect faults, rows/entities
            ├── pagination.py     # keyset + offset paging
            └── errors.py         # exception taxonomy → friendly messages
```

**Dependencies**

| Package | Why |
|---|---|
| `fastmcp>=3.4` | Standalone FastMCP — first-class stdio *and* streamable HTTP, needed for the eventual hosted deployment |
| `httpx` | HTTP client with timeouts, connection reuse, cookie handling |
| `lxml` | Real XPath over SOAP responses; preserves raw XML fidelity for entity documents |
| `pydantic-settings` | Typed env config with fail-fast validation |
| `python-dotenv` | `.env` loading in dev |

---

## 4. Tools (Phase 1)

Six tools — the design doc's three, plus three justified above.

### 4.1 `test_connection`
**Input:** none.
Runs `Logon`, reports instance URL, resolved auth mode, whether the session was obtained, server build number if exposed, and round-trip latency. Never echoes tokens. First thing to run when creds land.

### 4.2 `list_schemas`
**Input:** `namespace?` (filter, e.g. `cus`), `include_builtin?` (default `true`), `mapping_type?`.
**Call:** `xtk:queryDef#ExecuteQuery` on `xtk:schema`, selecting `@name @namespace @label @mappingType`, ordered by `@namespace, @name`.
**Returns:** JSON array.

> **Verified.** `@extendedSchema` does **not** exist on `xtk:schema` — selecting it fails with `XTK-170036 Unable to parse expression '@extendedSchema'`. It lives on `xtk:srcSchema`. Extension detection therefore happens via `get_schema_definition(form="source")`, not at listing time.
>
> ACC also returns attributes you did not select — `label`, `img`, `md5`, `_cs`, `_isMemoNull`. The `md5` is free and is exactly the cache key Phase 2 needs.

### 4.3 `get_schema_definition`
**Input:** `schema` (e.g. `nms:recipient`), `form` = `source` | `compiled` (default) | `wsdl`.

| `form` | Call | Verified |
|---|---|---|
| `inventory` **(default)** | derived from `compiled`; compact JSON, not XML | ✅ 10k / 42k / 13k for recipient / delivery / workflow |
| `compiled` | `xtk:persist#GetEntityIfMoreRecent` with `strPk="xtk:schema\|<schema>"` | ✅ 18.6 KB for `nms:recipient` |
| `source` | `xtk:persist#GetEntityIfMoreRecent` with `strPk="xtk:srcSchema\|<schema>"` | ✅ 11.7 KB for `nms:recipient` |
| `wsdl` | authenticated `GET schemawsdl.jsp?schema=<schema>` | ✅ 302 without session → 200 with |

**Returns:** raw XML string + metadata.

> **Corrected against the live WSDL.** The method is on **`xtk:persist`**, not `xtk:session`, and its parameters are **`strPk` / `strMd5` / `bMustExist`** — not `pk`/`md5`/`mustExist`. Using `urn:xtk:session` yields `SOP-330024 Unspecified function library`. Adobe's published API reference lists it under `xtk:session`; the instance's own generated WSDL says `soapAction="xtk:persist#GetEntityIfMoreRecent"`. **Trust the instance WSDL.**

### 4.4 `query_schema`
**Input:** `schema`, `fields[]`, `where?`, `order_by?`, `page_size?` (default 200), `cursor?`.
**Call:** `ExecuteQuery` with `operation="select"`.
**Returns:** `{rows: [...], next_cursor, truncated, returned, note}`.

Paging uses keyset on `@id` by default (`@id > <last>`, ascending) because offset paging on a live table silently skips or duplicates rows; falls back to `startLine`/`lineCount` for schemas without a usable `@id`. An `orderBy` is always injected — the design doc omits this and it is the single most common cause of bad pagination.

### 4.5 `get_entity` *(new)*
**Input:** `entity_key` (e.g. `xtk:srcSchema|cus:loyalty`, `nms:deliveryMapping|mapRecipient`), `must_exist?`.
**Call:** `xtk:persist#GetEntityIfMoreRecent` (`strPk` / `strMd5` / `bMustExist`).
**Returns:** the full entity XML.

**Verified working** for `xtk:schema`, `xtk:srcSchema`, `nms:deliveryMapping`. This is what schema-level dependency mapping runs on.

> **Known constraint.** The key resolver builds `where @name = '<value>'`, so `get_entity` only works for entities whose schema has a `@name` attribute. `xtk:workflow` has none — it uses `@internalName` — so every key form fails with *"Attribute 'name' unknown (see definition of schema 'Workflows (xtk:workflow)')"*, including numeric ids and expression syntax. The tool should detect this class of schema and return an actionable message rather than a raw fault.

### 4.6 `count_records` *(new)*
**Input:** `schema`, `where?`.
**Call:** `ExecuteQuery` with `operation="count"`.
**Returns:** `{schema, where, count}`.

---

## 5. ACC client behaviour

**Auth.** `Logon` → cache `pstrSessionToken` + `pstrSecurityToken` in memory only. Every subsequent call carries `SOAPAction: <schema>#<Method>`, `X-Security-Token`, `Cookie: __sessiontoken=…`, and the token in the body element. On a session-expiry fault (`XSV-350008`, or a fault body matching `/session.*expired|invalid session/i` — code strings vary by build), re-logon once transparently and retry; a second failure propagates. Tokens are never logged, never in tool output, and are redacted from exception strings.

**Faults.** SOAP faults arrive with HTTP 200. Every response goes through `raise_for_fault()` before parsing. Faults map to a typed exception hierarchy → friendly, actionable text (e.g. *"The API account lacks read rights on `cus:loyaltyTier`."*), with the raw fault text preserved so the agent can self-correct a bad XPath.

**Retries.** Bounded exponential backoff on connection errors, timeouts, 5xx and 429 (honouring `Retry-After`). Max 3 attempts. Never retry auth failures or malformed queries.

**Output caps.** Every tool result passes through `formatting.py`: max 500 rows and max 50,000 characters per call. On truncation the payload is cut and an explicit `{"truncated": true, "returned": N, "note": "…use cursor/page_size or narrow fields"}` is attached, so the agent knows it saw a partial result rather than assuming completeness.

**Adapter seam.** `AccAdapter` protocol (`list_schemas`, `get_schema_definition`, `query`, `count`, `get_entity`, `ping`). `Acc7Client` implements it. Tools depend only on the protocol — a v8 REST adapter later drops in without touching the MCP layer.

---

## 6. Configuration

`.env.example` (committed) — fill in and save as `.env` (gitignored):

```
# --- Instance ---
ACC_BASE_URL=https://<stage-host>          # scheme+host only, no /nl/jsp path
ACC_AUTH_MODE=native                        # native | ims
ACC_LOGIN=
ACC_PASSWORD=

# --- IMS (leave blank unless the stage instance is IMS-fronted) ---
ACC_IMS_ENDPOINT=https://ims-na1.adobelogin.com
ACC_IMS_CLIENT_ID=
ACC_IMS_CLIENT_SECRET=
ACC_IMS_ORG_ID=
ACC_IMS_SCOPES=

# --- Behaviour ---
ACC_TIMEOUT_SECONDS=60
ACC_DEFAULT_PAGE_SIZE=200
ACC_MAX_ROWS=500
ACC_MAX_RESPONSE_CHARS=50000
ACC_MAX_RETRIES=3
ACC_VERIFY_TLS=true
ACC_LOG_LEVEL=INFO

# --- Transport ---
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
```

`.gitignore` excludes `.env`, `__pycache__/`, `.venv/`, `*.egg-info`, and any `*.xml` scratch dumps.

---

## 7. Deviations from `acc7-mcp-server-plan.md`

Mirrored into `DEVIATIONS.md` in the repo.

1. **Python + FastMCP** instead of Node/TypeScript.
2. **Auth transport fixed** — the doc omits the `__sessiontoken` cookie; it is required alongside `X-Security-Token`.
3. **Auth made pluggable** — `Authenticator` interface, native now, IMS stub, env-selected.
4. **`get_schema_definition` re-based off WSDL.** The doc calls `schemawsdl.jsp` the mandatory source for the v7→v8 structural audit. It isn't — it emits SOAP method signatures, not fields, types, links, indexes or `extendedSchema`. Now defaults to the compiled schema via `GetEntityIfMoreRecent`, with `source` and `wsdl` as options.
5. **`schemawsdl.jsp` calls are authenticated.** The doc's sample code does a bare unauthenticated GET, which returns 401.
6. **Added `get_entity`** — dependency mapping is unreachable without full entity XML.
7. **Added `count_records`** — the doc lists count queries as a use case but `query_schema` as specified cannot express them.
8. **Added `test_connection`** — needed to validate the stage instance the moment access lands.
9. **Pagination changed** to keyset-by-`@id` with offset fallback, always with an injected `orderBy`. The doc's `startLine`/`lineCount`-without-sort is unreliable on live tables. The doc's "`SelectAll` + session-held cursor" description is also incorrect — `SelectAll` selects all *columns*; it is not a cursor mechanism.
10. **Hard output caps** (500 rows / 50k chars) with explicit truncation markers — the doc has none, and an uncapped result floods the agent's context.
11. **Response format split** — JSON for tabular, raw XML for documents.
12. **Rate-limit section re-scoped** — the doc's HTTP 429 discussion is v8 Managed Services behaviour. A v7 instance more likely constrains via session limits and app-server timeouts. Backoff still handles 429 if present.
13. **Doc fix:** `@scheduling` is not an attribute on `nms:delivery`; scheduling is a child element.
14. **Phase 1 ships 6 tools, not 3.** Phases 2 and 3 unchanged and untouched.

**Added after live verification (2026-08-06):**

15. **`GetEntityIfMoreRecent` is on `xtk:persist`, not `xtk:session`,** with params `strPk` / `strMd5` / `bMustExist`. Adobe's published reference is misleading; the instance WSDL is authoritative.
16. **`@extendedSchema` removed from `list_schemas`** — it does not exist on `xtk:schema`.
17. **`get_entity` is limited to `@name`-keyed schemas.** Workflows are excluded; the tool must say so clearly instead of surfacing a raw fault.
18. **`md5` comes back free on schema rows** — adopt it as the Phase 2 cache key rather than inventing one.

**Added 2026-08-24:**

21. **`get_schema_definition` defaults to an `inventory` form**, not raw XML. The raw compiled document is 236k for `nms:delivery` and 535k for `xtk:workflow`, so the response cap truncated them to 21% and 9% — mid-element, therefore unparseable. The inventory carries what an audit needs (fields with types, links with join conditions, keys, indexes, referenced enumerations) at 10k–42k, and is honest about what it leaves out: memo structures are named with declared-field counts, and any budget trimming is listed in `trimmed`. Raw `compiled`/`source`/`wsdl` remain available.
22. **`ACC_MAX_RESPONSE_CHARS` raised 50k → 120k.** The cap is ours, not ACC's or MCP's — ACC returns 535k documents happily and MCP has no limit. The real constraint is context budget. 120k (~30k tokens) lets the largest schema's complete inventory through untrimmed; sampling 24 sql-mapped schemas gave a median of 4,684 chars, so `nms:delivery` and `xtk:workflow` are ~50x outliers, not the norm.
23. **The inventory is complete, not selective.** Every field is listed, memo-mapped ones flagged `"xml": true`; every enumeration is included, referenced or not. An earlier version listed only SQL fields and referenced enums — defensible for size, wrong for an audit that has to account for what exists.

**Added 2026-08-10:**

19. **Transport is selectable again** via `MCP_TRANSPORT`, defaulting to `http`. Deleting stdio outright cost more than parity gained: the server had to be started by hand, and an orphaned process on port 8000 would silently serve stale code.
20. **Connection resolution is transport-aware.** stdio has no HTTP headers, so the environment is the only channel and is read unconditionally there — this is not a hole in the multi-tenant rule, because Claude launches one stdio process per client with its own env. Under http, headers stay required unless `ACC_ALLOW_ENV_FALLBACK=true`. Error text follows suit, naming env vars under stdio and headers under http.

---

## 8. Build order

Commit at each numbered step.

1. `git init`; `pyproject.toml`, `.gitignore`, `.env.example`, README skeleton, `DEVIATIONS.md`.
2. `config.py` — typed settings, fail-fast on missing required vars with a message naming the var.
3. `acc/transport.py` — httpx client, retry/backoff, timeouts, token redaction in logs.
4. `acc/v7/envelopes.py` + `parser.py` + `errors.py` — envelope builders and fault handling. Envelopes follow §5 of the design doc verbatim, plus the cookie/security-header fix.
5. `acc/v7/auth.py` — `NativeLogonAuth` with transparent re-logon; `ImsAuth` raising a clear "not yet implemented, set `ACC_AUTH_MODE=native`" until the instance says otherwise.
6. `acc/v7/client.py` + `pagination.py` — implement `AccAdapter`.
7. `formatting.py` — caps and truncation envelope.
8. `tools/*` + `server.py` — register six tools, serve streamable HTTP.
9. README — setup, env vars, `claude mcp add --transport http` snippet, tool reference, known-unknowns list.

---

## 9. Verification

### Already proven at the raw-SOAP level (2026-08-06)

Every call below was executed against the live instance before any code was written.

| # | Check | Result |
|---|---|---|
| 1 | `GET /r/test` | `status='OK' version='7.4.3' build='9396' instance='partners'` |
| 2 | `xtk:session#Logon` as `mcp_api` | HTTP 200, 39-char session token, 109-char security token |
| 3 | `ExecuteQuery` on `xtk:schema` | rows returned (after dropping `@extendedSchema`) |
| 4 | `ExecuteQuery` `operation="count"` on `nms:recipient` | `count="1"` |
| 5 | `ExecuteQuery` on `xtk:workflow` | real rows — `cleanup`, `tracking`, `billing`, `offerMgt`, `stockMgt`… |
| 6 | `schemawsdl.jsp` without / with session | 302 → 200 (65 KB) |
| 7 | `xtk:persist#GetEntityIfMoreRecent` on `xtk:srcSchema\|nms:recipient` | 11.7 KB source schema |
| 8 | same on `xtk:schema\|nms:recipient` | 18.6 KB compiled schema, 22 joins |
| 9 | same on `xtk:workflow\|<id>` | ❌ fails — see §10 |

The build therefore reproduces known-good calls rather than guessing at a contract.

### After the code exists

Re-run the same nine checks through the MCP tools and confirm identical results, then point Claude at the server and ask for a schema inventory plus a schema-level dependency map, spot-checked against the console.

---

## 10. Open questions

### Closed by the 2026-08-06 probe

- ~~**Hosting model**~~ — Adobe-hosted sandbox, instance `partners`.
- ~~**Network reachability**~~ — reachable, no VPN or IP allowlisting required.
- ~~**Instance build number**~~ — 7.4.3 / build 9396.
- ~~**Auth mode**~~ — native operator logon is available and is the Phase 1 target; see §2.6.
- ~~**A usable credential**~~ — native operator `mcp_api` created and verified. The Adobe ID could not authenticate (`XSV-350012`); the native operator can.
- ~~**API account rights**~~ — `mcp_api` can read `xtk:schema`, `xtk:srcSchema`, `xtk:workflow` and `nms:recipient`. Tighten to least-privilege before this points anywhere real.

### Nothing blocks Phase 1

### Still open

1. **Entity memo content — delivery configuration and workflow activities.** *(Spike run 2026-08-24; unresolved.)*

   Wider than previously recorded: the same mechanism blocks **deliveries** (`content`, `targets`, `mailParameters`, `tracking`, `properties`, `scheduling`) as well as workflow activities. This is the substance the testing team verifies, so it is the largest gap in the audit.

   **What the spike established:**

   | Finding | Evidence |
   |---|---|
   | ✅ Memo content **is** retrievable in principle | `xtk:persist#GetEntityIfMoreRecent` and `#LoadAsText` on `xtk:javascript\|nms:aaexception.js` returned 3,114 chars of actual JS source from the memo |
   | ❌ `queryDef` cannot reach it, four ways | selecting the container returns `<mailParameters/>` empty; selecting any leaf inside → *"Element 'var' unknown"* even though the compiled schema declares it; omitting `<select>` entirely returns a bare 222-char element; `fullLoad="true"` changes nothing |
   | ❌ The blocker is **key resolution**, not memo access | the resolver always builds `where @name = …`. `nms:delivery` and `xtk:workflow` have no `@name` (they use `@internalName`), so every pk form fails |
   | ❌ `Load` / `LoadIfExists` / `LoadAsText` share the resolver | for non-`@name` schemas they fall through to reading `datakit/eng/<type>/<id>.xml` off disk → `XFR-180000` / `XSV-350001` |
   | ❌ `nms:delivery#LoadExpandedContents` | `SOP-330011` with an empty detail |
   | ❌ `xtk:queryDef#GetXmlStruct` | echoes the queryDef back with `showSQL`; not entity content |

   **Conclusion:** not a memo problem — an addressing problem. Everything works for `@name`-keyed schemas and nothing works for the two that matter.

   **Untried, in order of promise:** capture what the client console actually sends when opening a delivery (it clearly can load them, so a working call exists); check whether a pk form derived from the schema's declared `<key>` is accepted; investigate JSSP endpoints outside `soaprouter.jsp`.

   **Partial fallback available:** `xtk:workflowTask` holds 40,522 rows on this instance with an `@activity` attribute and a link to `xtk:workflow`. Caveat — these are *execution* records, so they show which activities ran, not the workflow's definition or configuration.

2. **API account rights (tightening).** What to request: a dedicated technical operator (not a shared human login), with read access to the folders in scope and read rights on `nms:` / `cus:` schemas. Reading the system tables this server depends on — `xtk:schema`, `xtk:srcSchema`, `xtk:workflow` — usually needs elevated rights in practice; on a **stage** sandbox the pragmatic ask is an admin-group operator used read-only, then tighten to least-privilege before anything points at production. Verify against the instance rather than assuming.
3. **MCP-layer auth — now live, not hypothetical.** With stdio removed (2026-08-09), even local runs are an HTTP endpoint, and it is currently unauthenticated: anything that can reach the port inherits the service account's read access to the instance. Bound to `127.0.0.1` this is limited to processes on the dev machine, which is acceptable for a sandbox POC. **Before binding to anything other than loopback**, pick one: a bearer token on the transport, OAuth 2.1 (FastMCP has first-class helpers), or network-level restriction.
4. **Session TTL** — default 24h, configurable in `serverConf.xml`; transparent re-logon covers it either way.
5. **PII posture** — deferred to a later phase. Until then the server can read recipient data if asked; keep it pointed at stage.
