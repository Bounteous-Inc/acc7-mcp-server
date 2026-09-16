# ACCv7 MCP Server — Tool Reference

Read-only MCP server giving an AI agent live access to an Adobe Campaign Classic v7
instance over SOAP, for migration inventory, schema audit and dependency mapping.

**Generic:** works against any ACCv7 instance. Connection details are supplied per
client, never baked into the server. **Read-only:** no write path exists.

---

## 1. Connection

`MCP_TRANSPORT` selects the transport, and that determines how a client says which
instance it wants.

| `MCP_TRANSPORT` | Tenancy | Connection details from |
|---|---|---|
| `stdio` | single — one process per client | environment variables |
| `http` (default) | multi — many clients per process | `X-ACC-*` request headers |

```bash
# stdio
claude mcp add acc7 -s project \
  -e MCP_TRANSPORT=stdio \
  -e ACC_BASE_URL=https://<host> -e ACC_LOGIN=<operator> -e ACC_PASSWORD=<password> \
  -- /abs/path/.venv/bin/acc-mcp-server

# http  (start the server first)
claude mcp add --transport http acc7 http://127.0.0.1:8000/mcp -s project \
  --header "X-ACC-Base-Url: https://<host>" \
  --header "X-ACC-Login: <operator>" \
  --header "X-ACC-Password: <password>"
```

Credentials never pass through tool arguments, so they never enter the model's context.

### Authentication is internal — there is no `logon` tool

`xtk:session#Logon` fires lazily from `_ensure_session()` before the first ACC call;
the rest of the session reuses it. It isn't exposed as a tool because the agent has
nothing to pass it (credentials come from `.env` / headers, never tool arguments) and
nothing useful to do with the result (it can't attach the resulting headers itself —
the client does). Exposing it would only let the agent forget to call it, call it
redundantly, or try to manage expiry itself. Session state belongs to the server, not
the conversation.

`test_connection` is a **diagnostic**, not "the logon tool" — it forces a fresh
`Logon` and reports on it, so a failure there is unambiguously auth/connectivity
rather than a bad query. Every other tool logs on too, the first time.

Both a session token and a security token come back from `Logon` and are required on
**every** subsequent call:

```
Content-Type:     text/xml; charset=UTF-8
SOAPAction:       <schema>#<Method>
X-Security-Token: <securityToken>
Cookie:           __sessiontoken=<sessionToken>
```

Tokens live **in memory only, for the process lifetime** — no disk, no cache file, no
`.env` write-back. A logging filter redacts token patterns so an accidental debug log
can't leak them, and they're stripped from exception strings before reaching the
agent. An `asyncio.Lock` guards re-logon, since tool calls run concurrently and an
expired session would otherwise trigger simultaneous `Logon`s. Expiry is fault-driven
rather than clock-driven: on ACC's session-expiry fault, the client re-logs on once
transparently and retries; a second failure propagates. The login must be a **native
operator** — an Adobe ID cannot use the SOAP API.

Under `http`, one process holds one service account's tokens shared across every
caller reaching the endpoint — see [Known limitations](#5-known-limitations).

---

## 2. Tools

| Tool | Purpose | Status |
|---|---|---|
| `test_connection` | Instance reachable + credentials valid | done |
| `list_schemas` | Discovery — every schema in the instance | done |
| `get_schema_definition` | Structure of one schema | done |
| `count_records` | Row count without transferring rows | done |
| `query_schema` | Read records from any schema | planned — see `NEXT-STEPS.md` |
| `get_entity` | One entity in full, including its memo blob | planned — see `NEXT-STEPS.md` |

Typical order: `test_connection` → `list_schemas` → `count_records` (drop empty tables)
→ `get_schema_definition` → `query_schema` / `get_entity`.

---

### 2.1 `test_connection` ✅

Verifies the instance answers and the credentials work. Makes no query, so a failure is
unambiguously auth or connectivity rather than a bad request. Run it first when
debugging.

**Input:** none.

```json
{
  "isError": false,
  "instance_url": "https://ac984us.adobesandbox.com",
  "login": "mcp_api",
  "auth_mode": "native",
  "session_established": true,
  "logon_latency_ms": 932.4,
  "instance": { "version": "7.4.3", "build": "9396", "name": "partners", "status": "OK" },
  "operator": { "userInfo.login": "mcp_api", "userInfo.loginId": "5471" }
}
```

---

### 2.2 `list_schemas` ✅

The discovery entry point. Nothing is hardcoded, so it works against any instance.
Use `include_builtin: false` to see only what a client built — that is the migration scope.

**Input**

| Field | Type | Default | Meaning |
|---|---|---|---|
| `namespace` | string? | — | Filter to one namespace, e.g. `cus` |
| `include_builtin` | bool | `true` | Include Adobe's reserved namespaces (`xtk`, `nl`, `nms`, `ncm`, `temp`, `ncl`, `crm`, `xxl`) |

```json
// request
{ "namespace": "nms" }

// response
{
  "isError": false,
  "returned": 99, "total_available": 99, "truncated": false,
  "namespaces": ["nms"],
  "filter": { "namespace": "nms", "include_builtin": true },
  "rows": [
    { "schema": "nms:activeContact", "namespace": "nms", "name": "activeContact",
      "label": "Active contacts", "mappingType": "sql", "md5": "50AE4E11…" }
  ]
}
```

`md5` changes when the schema changes — free drift detection, and the intended Phase 2
cache key.

---

### 2.3 `get_schema_definition` ✅

The structure of one schema. Four forms; `inventory` is the default and the one to use
unless raw XML is genuinely needed.

**Input**

| Field | Type | Default | Meaning |
|---|---|---|---|
| `schema` | string | — | Qualified name, e.g. `nms:recipient` |
| `form` | enum | `inventory` | `inventory` \| `compiled` \| `source` \| `wsdl` |

| Form | Returns |
|---|---|
| `inventory` | Compact JSON: every field with type, links with join conditions, keys, indexes, enumerations |
| `compiled` | Raw effective XML — base schema merged with all extensions, SQL names resolved |
| `source` | Raw authored XML — for an extension, only the added fields plus `extendedSchema` |
| `wsdl` | SOAP method signatures only |

```json
// request
{ "schema": "nms:typology" }

// response (abridged)
{
  "isError": false, "form": "inventory",
  "schema": "nms:typology", "label": "Typologies", "sqltable": "NmsTypology",
  "counts": { "fields": 10, "sql_fields": 10, "xml_fields": 0,
              "links": 7, "keys": 2, "indexes": 5, "enumerations": 1 },
  "fields": [
    { "name": "name", "type": "string", "length": 64, "label": "Internal name",
      "sqlname": "sName", "required": true }
  ],
  "links": [
    { "name": "createdBy", "target": "xtk:operator",
      "joins": [{ "src": "@createdBy-id", "dst": "@id" }], "label": "Created by" }
  ],
  "keys": [ { "name": "id", "fields": ["@id"], "internal": true } ],
  "enumerations": { "typologyType": [ { "value": "0", "name": "campaign", "label": "Campaign" } ] }
}
```

Fields marked `"xml": true` are stored inside an XML document in a memo column rather
than their own SQL column. They **cannot** be selected with `query_schema`; use
`get_entity`. Links are the dependency edges for mapping.

The `compiled` and `source` forms return XML as a string with a `truncated` flag —
truncation cuts mid-element, so check the flag before parsing. The `inventory` form
exists specifically because the raw compiled document is 236 KB for `nms:delivery`
and 535 KB for `xtk:workflow` — too large to return whole and useless truncated
mid-element. The inventory lists every field (memo-mapped ones flagged, not omitted)
and every enumeration, so it's complete for audit purposes even though it isn't raw.

---

### 2.4 `count_records` ✅

Counts rows in the database without transferring them. Use it to size the migration,
spot dead objects (a custom table with 0 rows is one you do not migrate) and reconcile
before/after. Prefer it over counting `query_schema` results, which may be paged.

**Input**

| Field | Type | Default | Meaning |
|---|---|---|---|
| `schema` | string | — | Qualified name |
| `where` | string? | — | XPath-style condition |

```json
// request
{ "schema": "nms:delivery", "where": "@status = 0" }

// response
{ "isError": false, "schema": "nms:delivery", "where": "@status = 0", "count": 29 }
```

---

### 2.5 `query_schema` ⏳ planned

Reads records from any schema. Intended for configuration objects — folders, forms,
options, operators, typology rules, deliveries, workflows — and for sampling custom
tables. **Selects SQL columns only.**

**Input**

| Field | Type | Default | Meaning |
|---|---|---|---|
| `schema` | string | — | Qualified name |
| `fields` | string[] | — | XPath-style expressions, e.g. `["@internalName", "@label"]` |
| `where` | string? | — | XPath-style condition |
| `order_by` | string[]? | — | Sort expressions |
| `page_size` | int? | 200 | Rows per page; server-capped |
| `cursor` | string? | — | Opaque cursor from a previous `next_cursor` |

```json
// request
{ "schema": "xtk:workflow", "fields": ["@internalName", "@label", "@state"],
  "where": "@state = 11", "page_size": 50 }

// response
{
  "isError": false, "schema": "xtk:workflow",
  "rows": [ { "id": "1764", "internalName": "cleanup", "label": "Database cleanup", "state": "11" } ],
  "returned": 16, "truncated": false,
  "next_cursor": "eyJtIjoiayIsImxhc3QiOjE3Nzh9",
  "paging": "keyset"
}
```

Paging is **keyset** on `@id` by default — ACC's `startLine` is off by one and
duplicates a row at each page boundary (measured on `xtk:workflowTask`: `startLine=0`
and `startLine=5` both return id `1120`). `@id` is auto-added to the select to build
the cursor. Offset paging is used only when the schema has no `@id` or the caller
supplies `order_by`; in those cases `paging` is `"offset"` and a note flags the
duplicate risk.

---

### 2.6 `get_entity` ⏳ planned

Returns one entity's complete XML document, **including memo-stored content** that
`query_schema` cannot reach. This is where a delivery's content and scheduling, or a JS
library's source, actually live.

**Input**

| Field | Type | Default | Meaning |
|---|---|---|---|
| `entity_key` | string | — | `schema\|name` |
| `must_exist` | bool | `true` | Fail if absent rather than returning empty |

```json
// request
{ "entity_key": "xtk:javascript|nms:aaexception.js" }

// response
{
  "isError": false,
  "entity_key": "xtk:javascript|nms:aaexception.js",
  "chars": 3526, "truncated": false,
  "xml": "<javascript name=\"aaexception.js\" namespace=\"nms\">…<data><![CDATA[ /* source */ ]]></data></javascript>"
}
```

**Key formats differ by schema.** Schemas carrying a `namespace` field want
`namespace:name` (`xtk:javascript`, `xtk:form`); the rest take a bare name
(`xtk:folder|aggregates`, `nms:typology|defaultTypology`).

**Limitation:** `nms:delivery` and `xtk:workflow` cannot be fetched — ACC's key resolver
matches on `@name` and neither has it. Use `query_schema` for their metadata. This is the
one open gap in the server (see `DESIGN.md` §Open questions for the full spike writeup).

---

## 3. Error contract

Every tool reports failures as data, never as a bare string, so an agent can decide what
to do without guessing:

```json
{
  "isError": true,
  "isRetryable": false,
  "errorType": "QueryError",
  "message": "Invalid schema name 'bogus'. Expected 'namespace:name', e.g. 'nms:recipient'. Call list_schemas to find the qualified name.",
  "retryGuidance": "Do not retry unchanged — correct the request first."
}
```

Retryable errors also carry `retryAfterSeconds`. Successful results carry `"isError": false`.
SOAP faults arrive with HTTP 200, never as an HTTP error code — every response is checked
for a fault before use.

| `errorType` | Retryable | Meaning |
|---|---|---|
| `ConfigurationError` | no | Missing/invalid connection details |
| `AuthError` | no | Credentials rejected |
| `PermissionError_` | no | Operator lacks rights on the object |
| `QueryError` | no | Malformed field or condition — correct and retry |
| `SchemaNotFoundError` | no | Schema or entity does not exist |
| `EntityKeyError` | no | Schema not addressable by entity key |
| `SessionExpiredError` | yes (0s) | Session renewed; retry now |
| `TransportError` | yes (5s) | Instance unreachable |
| `AccError` | no | Unclassified instance fault — read the message |

---

## 4. Configuration

Server-level settings (environment or `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `http` | `stdio` \| `http` |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `127.0.0.1` / `8000` | HTTP bind |
| `ACC_TIMEOUT_SECONDS` | `60` | Per-request timeout |
| `ACC_DEFAULT_PAGE_SIZE` | `200` | Default rows per page |
| `ACC_MAX_ROWS` | `500` | Hard row ceiling per call |
| `ACC_MAX_RESPONSE_CHARS` | `120000` | Response budget — a context guard, not an ACC limit |
| `ACC_MAX_RETRIES` | `3` | Bounded retries with backoff |
| `ACC_VERIFY_TLS` | `true` | Disable only for local debugging |
| `ACC_MAX_CACHED_CONNECTIONS` | `32` | LRU cap on per-tenant clients |
| `ACC_LOG_LEVEL` | `INFO` | Server log verbosity |

Connection settings (`ACC_BASE_URL`, `ACC_LOGIN`, `ACC_PASSWORD`, `ACC_AUTH_MODE`) apply
under `stdio`. Under `http` they are ignored unless `ACC_ALLOW_ENV_FALLBACK=true`.

---

## 5. Known limitations

- **Memo content for deliveries and workflows is unreachable.** Their configuration —
  delivery `content`/`scheduling`/`typologyFilter`, workflow activities — lives in an XML
  memo column addressable only by entity key, and ACC's resolver requires `@name`, which
  neither schema has. Every other configuration schema is reachable. Full investigation
  in `DESIGN.md`.
- **`query_schema` sees SQL columns only.** On `nms:delivery` that is 234 of 581 fields.
- **Response budget.** Raw `compiled` XML for the largest schemas exceeds the cap and is
  truncated; use `form: "inventory"`.
- **OAuth Server-to-Server is not implemented.** It requires `nlserver config -setimsoauth`
  on the instance host. Native operator logon is the supported path.
- **No MCP-layer authentication.** Under `http`, anything that can reach the endpoint
  inherits the service account's read access. Bind to loopback, or add transport auth
  before exposing it.

See `DESIGN.md` for the reasoning behind these tradeoffs and the full open-questions list.
