# ACCv7 MCP Server — Quick Reference

**Instance:** `https://ac984us.adobesandbox.com` · v7.4.3 · build 9396 · Adobe-hosted `partners` sandbox
**Auth:** native operator `mcp_api` · verified 2026-08-06
**Status:** Ready to build — 5 of 6 tools proven at raw-SOAP level

---

## 1. Authentication (server-internal, not an agent tool)

Happens automatically before the first ACC call and is transparent thereafter.

```
POST /nl/jsp/soaprouter.jsp
SOAPAction: xtk:session#Logon
  <strLogin>mcp_api</strLogin>
  <strPassword>…</strPassword>
      ↓
  <pstrSessionToken>   (39 chars)
  <pstrSecurityToken>  (109 chars)
```

Both tokens are then required on **every** subsequent call:

```
Content-Type:     text/xml; charset=UTF-8
SOAPAction:       <schema>#<Method>
X-Security-Token: <securityToken>
Cookie:           __sessiontoken=<sessionToken>
```

### Why there is no `logon` tool

`Logon` is a method on the client, not an MCP tool. It fires lazily from `_ensure_session()` before any ACC call — first tool call of the process triggers it, the rest reuse the session.

It is not exposed because the agent has nothing to pass to it (credentials come from `.env`) and nothing useful to do with the result (it cannot attach headers — the client does). Exposing it would only let the agent forget to call it, call it redundantly, or attempt to manage expiry itself. Session state belongs to the server, not the conversation.

`test_connection` is a **diagnostic**, not "the logon tool": its purpose is *"is the instance reachable and are the configured credentials valid?"*, and it answers that by forcing a fresh `Logon`. Every other tool logs on too, the first time — `test_connection` just does nothing else, so a failure is unambiguously auth or connectivity rather than a bad query.

### Token storage

**In memory, on the client instance, for the process lifetime. Nowhere else.**

```python
@dataclass
class SessionState:
    session_token:  str | None = None   # → Cookie: __sessiontoken=…
    security_token: str | None = None   # → X-Security-Token: …
    obtained_at:    float | None = None
```

- **No disk** — no cache file, no `.env` write-back, no temp file. Process dies, tokens die.
- **No logs** — a logging filter redacts token patterns, so an accidental `logger.debug(response)` cannot leak them.
- **No tool output** — never returned; stripped from exception strings before they reach the agent.
- **`asyncio.Lock` around re-logon** — FastMCP runs tool calls concurrently; without it, parallel calls on an expired session fire simultaneous `Logon`s.
- **Expiry is fault-driven, not clock-driven** — `obtained_at` is recorded and 24h treated as a hint, but the authoritative signal is ACC's session-expiry fault, since the TTL is server-configurable. On that fault: re-logon once transparently and retry; a second failure propagates.

> **Later, under HTTP transport:** one process holds one service account's tokens, shared across every caller reaching the endpoint. That is the endpoint-auth problem (`plan.md` §10, open question #3), not a token-storage problem.

**Not used:** OAuth Server-to-Server (needs `nlserver config -setimsoauth:…` on the instance host — an Adobe request on a hosted sandbox). Legacy per-call login+password is unsupported.

---

## 2. Tools

| # | Tool                      | Input                                                                          | Returns                | Purpose                                            | Status                                                    |
| - | ------------------------- | ------------------------------------------------------------------------------ | ---------------------- | -------------------------------------------------- | --------------------------------------------------------- |
| 1 | `test_connection`       | —                                                                             | instance info, latency | Diagnostic: instance reachable + credentials valid | ✅                                                        |
| 2 | `list_schemas`          | `namespace?`, `include_builtin?`                                           | JSON rows              | Discovery — what exists                           | ✅                                                        |
| 3 | `count_records`         | `schema`, `where?`                                                         | `{count}`            | Sizing; 0 rows = dead table                        | ✅                                                        |
| 4 | `get_schema_definition` | `schema`, `form`=`compiled`\|`source`\|`wsdl`                        | raw XML                | Field/type/join/extension detail                   | ✅                                                        |
| 5 | `query_schema`          | `schema`, `fields`, `where?`, `order_by?`, `page_size?`, `cursor?` | JSON rows              | Config reads — workflows, deliveries              | ✅                                                        |
| 6 | `get_entity`            | `entity_key`, `must_exist?`                                                | raw entity XML         | Full entity body for dependency edges              | ⚠️`@name`-keyed schemas only; **not** workflows |

### Underlying calls

| Tool                      | SOAP / HTTP                                                                                                                       |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `test_connection`       | `xtk:session#Logon`                                                                                                             |
| `list_schemas`          | `xtk:queryDef#ExecuteQuery` on `xtk:schema`                                                                                   |
| `count_records`         | `xtk:queryDef#ExecuteQuery`, `operation="count"`                                                                              |
| `get_schema_definition` | `xtk:persist#GetEntityIfMoreRecent` (`strPk`/`strMd5`/`bMustExist`); `wsdl` form = authenticated `GET schemawsdl.jsp` |
| `query_schema`          | `xtk:queryDef#ExecuteQuery`, `operation="select"`                                                                             |
| `get_entity`            | `xtk:persist#GetEntityIfMoreRecent`                                                                                             |

**Output format:** JSON for tabular results (tools 2, 3, 5), raw XML inside a thin JSON wrapper for entity documents (tools 4, 6). Caps: 500 rows / 50,000 chars per call, with an explicit truncation marker.

---

## 3. Resources — Phase 2, not now

`acc7://schema/<ns>:<name>` per schema, cached and invalidated on the `md5` attribute that already comes back free on every `xtk:schema` row. Removes repeat SOAP round-trips for structure the agent references often.

---

## 4. Agent call order

```
0  [auto] xtk:session#Logon                      → session + security tokens (server-internal)

── discovery ──
1  test_connection                               → confirm session, instance identity
2  list_schemas                                  → full inventory
3  list_schemas(namespace="cus")                 → custom surface = migration scope
4  count_records per cus: schema                 → drop the empty ones

── per surviving schema ──
5  get_schema_definition(form="source")          → what the client customised (@extendedSchema)
6  get_schema_definition(form="compiled")        → effective structure + <join> edges

── config inventory ──
7  query_schema("xtk:workflow", [@internalName, @label, @state])
8  query_schema("nms:delivery",  [@internalName, @label, @status])
9  get_entity(...) on @name-keyed entities       → deeper bodies

10 agent builds the dependency graph from 6 + 9
```

Steps 2–4 are cheap and narrow scope before the expensive per-schema calls at 5–6. Schema-level dependency mapping completes at step 10; workflow-level stops at step 7 until the open item below is resolved.

---

## 5. Key gotchas (learned the hard way)

| Gotcha                            | Detail                                                                                                                                     |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `GetEntityIfMoreRecent` service | It's**`xtk:persist`**, not `xtk:session`. Wrong one → `SOP-330024 Unspecified function library`                                     |
| Its parameters                    | **`strPk` / `strMd5` / `bMustExist`** — not `pk`/`md5`/`mustExist`                                                      |
| Authoritative source              | Adobe's published API reference says`xtk:session`; the instance's generated WSDL says `xtk:persist`. **Trust the instance WSDL** |
| `@extendedSchema`               | Does not exist on`xtk:schema` (`XTK-170036`). Only on `xtk:srcSchema`                                                                |
| `schemawsdl.jsp`                | Requires a session — 302 without, 200 with                                                                                                |
| Faults                            | Arrive as HTTP**200** with a `SOAP-ENV:Fault` body. `SOP-330011` is a generic wrapper; the real message is in `<detail>`       |
| Pagination                        | `orderBy` is mandatory, or offset paging skips/duplicates rows. Default cap is 10,000 rows if `lineCount` is omitted                   |
| `SelectAll`                     | Selects all*columns*. It is **not** a cursor mechanism                                                                             |
| Extra attributes                  | ACC returns`label`, `img`, `md5`, `_cs`, `_isMemoNull` even when unselected                                                      |

---

## 6. Open item

**Workflow activity XML retrieval** — gates *workflow-level* dependency mapping only; schema-level is proven.

Ruled out with evidence:

- `xtk:persist#GetEntityIfMoreRecent` — key resolver builds `where @name = …`; `xtk:workflow` uses `@internalName` and has no `@name`
- `xtk:persist#Load` — reads `.xml` files from the server's `datakit/eng/workflow/` directory, not the database
- `queryDef` selecting `<node expr="activities"/>` — accepted but returns empty; everything beneath is `xml="true"` memo-mapped, not SQL-mapped

Untested leads: typed child elements of `activities` (`query`, `scheduler`, `js`, `jump`…) selected individually; the memo column itself; `xtk:workflowTask` for a per-activity view.

Handled as a timeboxed spike during implementation. If it stays unreachable, the plan degrades to workflow metadata plus `xtk:workflowTask`, stated explicitly rather than shipped as a silent gap.

---

## 7. Verified call log (2026-08-06)

| # | Check                                     | Result                                                                          |
| - | ----------------------------------------- | ------------------------------------------------------------------------------- |
| 1 | `GET /r/test`                           | `OK · 7.4.3 · 9396 · partners`                                             |
| 2 | `xtk:session#Logon` as `mcp_api`      | 200 · both tokens returned                                                     |
| 3 | `ExecuteQuery` on `xtk:schema`        | rows (after dropping`@extendedSchema`)                                        |
| 4 | `count` on `nms:recipient`            | `count="1"`                                                                   |
| 5 | `ExecuteQuery` on `xtk:workflow`      | `cleanup`, `tracking`, `billing`, `offerMgt`, `stockMgt`…            |
| 6 | `schemawsdl.jsp` without / with session | 302 → 200 (65 KB)                                                              |
| 7 | `xtk:srcSchema\|nms:recipient`           | 11.7 KB source schema                                                           |
| 8 | `xtk:schema\|nms:recipient`              | 18.6 KB compiled · 55 attributes ·**22 joins** · 13 keys · 12 indexes |
| 9 | `xtk:workflow\|<id>`                     | ❌ see §6                                                                      |

---

**Full detail:** `plan.md` (implementation plan) · `acc7-mcp-server-plan.md` §0 (errata vs. original design)

Lets start step by step while implementing. Do only the task that I asked you to do. After every step, I am going to review what you did and

  will guide for the next step.
