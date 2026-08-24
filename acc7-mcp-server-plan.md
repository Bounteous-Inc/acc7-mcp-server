| col1 | col2 | col3 |
| ---- | ---- | ---- |
|      |      |      |
|      |      |      |

# Adobe Campaign Classic v7 — MCP Server

## Architecture & Implementation Guide

**Version:** 1.1
**Date:** July 24, 2026 · **Errata added:** August 6, 2026
**Scope:** Read-focused MCP server connecting Claude to an Adobe Campaign Classic v7 instance over SOAP, for migration inventory/audit, dependency mapping, and analysis work.

---

## 0. Errata — verified against a live v7.4.3 instance

The following was probed against `https://ac984us.adobesandbox.com` (v7.4.3, build 9396, Adobe-hosted `partners` sandbox) on 2026-08-06. **Where this section contradicts the body of the document, this section wins.** The implementation plan in `plan.md` already reflects all of it.

| § | Document says | Instance says |
|---|---|---|
| 3.1 | Security token needed "for write operations, good practice throughout" | **Both** are mandatory on every authenticated call: `X-Security-Token: <securityToken>` header **and** `Cookie: __sessiontoken=<sessionToken>` |
| 3.1 note | IMS applies from v8.5.1+ | IMS expectation starts at **v7.3.1**; API integrations on **v7.4.1+** are steered to OAuth Server-to-Server. Native operator logon still works on 7.4.3 and is what we use |
| 4/6 | `schemawsdl.jsp` fetched unauthenticated | **Authenticated.** 302 redirect without a session, 200 with one |
| 5.2 | — | `@extendedSchema` does **not** exist on `xtk:schema` (`XTK-170036`). It lives on `xtk:srcSchema` |
| 5.4 | Deliveries select `@scheduling` | Not an attribute; scheduling is a child element |
| 5.5 | "`SelectAll` + session-held cursor" for paging | `SelectAll` selects all *columns*; it is not a cursor. Use `lineCount`/`startLine` **with an `orderBy`**, or keyset paging on `@id`. Default cap without `lineCount` is 10,000 rows |
| 6 | `get_schema_definition` returns WSDL, "mandatory for the v7→v8 audit" | WSDL gives SOAP *method signatures*, not fields/types/links. Use `xtk:persist#GetEntityIfMoreRecent` on `xtk:schema\|<name>` (compiled) or `xtk:srcSchema\|<name>` (source) |
| 6 | Three tools suffice | Dependency mapping additionally needs `get_entity`; counts need `count_records`; `test_connection` for diagnostics. Six total |

**Method correction that matters most:** `GetEntityIfMoreRecent` is reachable as **`xtk:persist#GetEntityIfMoreRecent`** with parameters **`strPk`, `strMd5`, `bMustExist`**. Calling it as `xtk:session#…` returns `SOP-330024 Unspecified function library`. Adobe's published API reference lists it under `xtk:session`; the instance's own generated WSDL is authoritative.

**Known limitation:** the entity key resolver builds `where @name = '<value>'`, so `GetEntityIfMoreRecent` works only for schemas that have a `@name` attribute. `xtk:workflow` uses `@internalName`, so **workflow activity XML is not retrievable this way**. `xtk:persist#Load` is also unusable — it reads `.xml` files from the server's `datakit` directory, not the database. Open item; see `plan.md` §10.

**Verified working:** `Logon` · `ExecuteQuery` select/count · `xtk:workflow` metadata queries · compiled and source schema retrieval (18.6 KB / 11.7 KB for `nms:recipient`, including 22 `<join>` dependency edges) · authenticated WSDL fetch.

---

## 1. Purpose & Context

This document describes how to build a Model Context Protocol (MCP) server that lets Claude (or any MCP-compatible client) read from an Adobe Campaign Classic v7 (ACCv7) instance.

The immediate driver is the ACCv7 → ACCv8 and SFMC → ACCv8 migration work: the MCP server replaces manual XML/CSV exports with live, on-demand access to workflows, deliveries, schemas, and targeting queries. Everything here is read-first; any future write capability is explicitly out of scope for Phase 1 and gated behind a human-executed package-promotion process.

### Key platform facts that shape the design

- **v7 is SOAP-only.** Campaign Classic predates REST as a standard and exposes no REST API. Every interaction is a SOAP call to `soaprouter.jsp`, or an HTTP GET against a JSSP page (e.g. the WSDL generator).
- **Schemas are the core abstraction.** Workflows, deliveries, recipients, and custom tables are all "schemas" — XML documents describing a database table. Discovering and reading schemas is the heart of the server.
- **Generic query API.** `xtk:queryDef.ExecuteQuery` reads records from *any* schema with a uniform request shape, so one code path handles all schemas.
- **Dynamic discovery makes it client-agnostic.** Because the server discovers schemas at runtime (querying `xtk:schema`), the same server works against any client's instance regardless of their custom schemas.

---

## 2. Architecture Overview

### 2.1 High-level component diagram

```
┌─────────────┐      MCP protocol      ┌──────────────────────────┐
│   Claude     │ ◄──────────────────► │      MCP Server            │
│  (client)    │   (stdio / HTTP)      │  (Node.js or Python)       │
└─────────────┘                        │                            │
                                        │  ┌──────────────────────┐  │
                                        │  │  MCP Layer            │  │
                                        │  │  - tools              │  │
                                        │  │  - resources          │  │
                                        │  └──────────┬───────────┘  │
                                        │  ┌──────────▼───────────┐  │
                                        │  │  ACC Client (adapter) │  │
                                        │  │  - session mgmt       │  │
                                        │  │  - SOAP builder       │  │
                                        │  │  - WSDL fetcher (HTTP) │  │
                                        │  │  - response parser     │  │
                                        │  │  - rate limiter        │  │
                                        │  └──────────┬───────────┘  │
                                        └─────────────┼──────────────┘
                                                      │ SOAP over HTTPS
                                                      │ (soaprouter.jsp)
                                                      │ + HTTP GET (schemawsdl.jsp)
                                        ┌─────────────▼──────────────┐
                                        │   ACCv7 Application Server   │
                                        │   (SOAP endpoint + DB)       │
                                        └──────────────────────────────┘
```

### 2.2 Layers

**MCP Layer** — Exposes tools (actions Claude invokes) and resources (data Claude can reference). This is the contract with Claude.

**ACC Client / Adapter** — All ACCv7-specific plumbing: authenticate, build SOAP envelopes, send them, parse responses, handle errors, respect rate limits. This is where the SOAP mess is contained. If you later add a v8 adapter (REST), it slots in beside this one behind the same MCP layer.

**Transport** — SOAP calls go to `https://<server>/nl/jsp/soaprouter.jsp`. WSDL generation is a plain HTTP GET to `https://<server>/nl/jsp/schemawsdl.jsp?schema=<schema>`.

### 2.3 Design principle: one server, pluggable version adapters

Even though Phase 1 is v7-only, structure the code so the version-specific client is an adapter behind a stable internal interface (`getSchemas()`, `queryData(schema, options)`, `getSchemaDefinition(schema)`). When v8 (REST) arrives, you add a v8 adapter without touching the MCP layer. Tools take an optional `version` parameter that routes to the right adapter.

---

## 3. Authentication & Session Management

ACCv7 authentication is a two-step SOAP dance against `xtk:session`.

### 3.1 Logon

Call the `Logon` method on `xtk:session`. It returns a **session token** and a **security token**. Both are required on subsequent calls:

- The **session token** goes in the SOAP body (`__sessiontoken`).
- The **security token** goes in the SOAP header (`X-Security-token`) for any write operation and is good practice to include throughout.

> **⚠️ Superseded by §0.** Both tokens are required on every authenticated call — `X-Security-Token` header **and** `Cookie: __sessiontoken=…`, not just for writes.
>
> **Note on IMS:** the IMS expectation begins at **v7.3.1**, not v8.5.1, and v7.4.1+ steers API integrations toward OAuth Server-to-Server. Native `Logon` still works on 7.4.3 and is what this build uses. The OAuth path additionally requires `nlserver config -setimsoauth:…` on the instance host — an Adobe support request on a hosted sandbox.

### 3.2 Session lifecycle

- Cache the session token in memory; reuse it across calls.
- Sessions expire — handle a re-logon transparently when a call returns a session-expiry error (`XSV-350008` or similar "session expired" fault).
- Never log tokens.

---

## 4. Step-by-Step Procedure

The core read workflow has one foundation step and three repeatable operations.

### Step 0 — Understand `xtk:queryDef` (foundation)

Pull the WSDL for the query API so you know the exact call structure:

```
GET https://<server>/nl/jsp/schemawsdl.jsp?schema=xtk:queryDef
```

This describes `ExecuteQuery` — the generic read method you'll use against every schema. Do the same for `xtk:session` (auth) and `xtk:persist` (base create/update/delete, only relevant if you later add writes).

### Step 1 — Discover all schemas (SOAP)

Use `xtk:queryDef.ExecuteQuery` to query the `xtk:schema` system table. This returns every schema in the instance — built-in (`nms:`, `xtk:`, `nl:`, `ncm:`) and custom (typically `cus:`). This is what makes the server client-agnostic: you never hardcode a schema list.

### Step 2 — (Optional) Pull schema definitions (HTTP GET)

For each schema you care about, fetch its WSDL / structure via `schemawsdl.jsp`. This tells you the fields, data types, and any schema-specific methods. Needed when you want to query specific fields intelligently, produce inventory/documentation, or map dependencies — **not** needed if you only want to bulk-read data with wildcard selects.

### Step 3 — Read data from each schema (SOAP)

Call `ExecuteQuery` per schema, selecting the fields you need with optional `where` conditions. Same request shape every time; only the schema name and selected nodes change.

### Sequence summary

```
0. GET  schemawsdl.jsp?schema=xtk:queryDef      → learn ExecuteQuery contract
1. SOAP xtk:session.Logon                        → session + security tokens
2. SOAP xtk:queryDef.ExecuteQuery on xtk:schema  → list of all schemas
3. (opt) GET schemawsdl.jsp?schema=<each>        → per-schema structure
4. SOAP xtk:queryDef.ExecuteQuery on <each>      → actual records
```

---

## 5. SOAP Call Structures

All SOAP calls are POSTed to `https://<server>/nl/jsp/soaprouter.jsp` with:

- `Content-Type: text/xml; charset=UTF-8`
- `SOAPAction: <schema>#<method>` (e.g. `xtk:session#Logon`)

### 5.1 Logon

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                   xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <Logon xmlns="urn:xtk:session"
           SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
      <sessiontoken xsi:type="xsd:string"></sessiontoken>
      <strLogin xsi:type="xsd:string">API_USER</strLogin>
      <strPassword xsi:type="xsd:string">API_PASSWORD</strPassword>
      <elemParameters xsi:type="ns:Element"
                      SOAP-ENV:encodingStyle="http://xml.apache.org/xml-soap/literalxml"/>
    </Logon>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

Response contains `<pstrSessionToken>` and `<pstrSecurityToken>`. Extract and cache both.

### 5.2 List all schemas (ExecuteQuery on xtk:schema)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                   xmlns:ns="http://xml.apache.org/xml-soap"
                   xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <ExecuteQuery xmlns="urn:xtk:queryDef"
                  SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
      <sessiontoken xsi:type="xsd:string">__SESSION_TOKEN__</sessiontoken>
      <entity xsi:type="ns:Element"
              SOAP-ENV:encodingStyle="http://xml.apache.org/xml-soap/literalxml">
        <queryDef operation="select" schema="xtk:schema" xtkschema="xtk:queryDef">
          <select>
            <node expr="@name"/>
            <node expr="@namespace"/>
            <node expr="@label"/>
          </select>
        </queryDef>
      </entity>
    </ExecuteQuery>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

### 5.3 Read records from a schema (ExecuteQuery on any schema)

Example: read recipients. Change `schema="..."` and the `<select>` nodes for other schemas — everything else stays identical.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                   xmlns:ns="http://xml.apache.org/xml-soap"
                   xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <ExecuteQuery xmlns="urn:xtk:queryDef"
                  SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
      <sessiontoken xsi:type="xsd:string">__SESSION_TOKEN__</sessiontoken>
      <entity xsi:type="ns:Element"
              SOAP-ENV:encodingStyle="http://xml.apache.org/xml-soap/literalxml">
        <queryDef operation="select" schema="nms:recipient" xtkschema="xtk:queryDef"
                  lineCount="200" startLine="0">
          <select>
            <node expr="@firstName"/>
            <node expr="@lastName"/>
            <node expr="@email"/>
          </select>
          <where>
            <condition expr="@email IS NOT NULL"/>
          </where>
        </queryDef>
      </entity>
    </ExecuteQuery>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>
```

### 5.4 Reading workflows and deliveries

Same call, different schema:

- **Workflows:** `schema="xtk:workflow"`, select `@internalName`, `@label`, `@state`.
- **Deliveries:** `schema="nms:delivery"`, select `@internalName`, `@label`, `@status`, `@scheduling`.

### 5.5 Pagination

`ExecuteQuery` returns bounded result sets — **10,000 rows by default if `lineCount` is omitted.** Use `lineCount` (page size) with `startLine` (offset), and **always include an `orderBy`**: offset paging without a deterministic sort silently skips or duplicates rows on a live table. Keyset paging on `@id` (`@id > <last>`, ascending) is more robust and is what the implementation uses.

> **⚠️ Corrected.** An earlier revision described a "`SelectAll` + session-held cursor" pattern. `xtk:queryDef#SelectAll` returns a queryDef with all *columns* selected — it is not a cursor mechanism.

---

## 6. MCP Interface: Tools & Resources

This is the contract the server exposes to Claude. Phase 1 ships three read-only tools; resources arrive in Phase 2.

### 6.1 Tools

#### `list_schemas`

Lists every schema (table) in the connected ACCv7 instance — built-in and custom. This is the discovery entry point; Claude calls it first to learn what the instance contains without any hardcoded assumptions.

- **Input:** none.
- **Output:** array of schema descriptors — `name`, `namespace`, `label` (e.g. `{ name: "recipient", namespace: "nms", label: "Recipients" }`).
- **Underlying call:** SOAP `xtk:queryDef#ExecuteQuery` against `xtk:schema`.
- **Typical use:** building the schema inventory that seeds the migration audit and dependency mapping.

#### `get_schema_definition`

Returns the structure of a single schema: its fields, data types, and any schema-specific methods. This is the mandatory step for the ACCv7 → ACCv8 structural analysis, since the migration insights come from schema structure rather than record data.

- **Input:**
  - `schema` *(string, required)* — fully qualified schema name, e.g. `nms:recipient`, `cus:loyaltyTier`.
- **Output:** the schema's WSDL / structure XML (fields, types, methods, extension relationships).
- **Underlying call:** HTTP GET to `schemawsdl.jsp?schema=<schema>`.
- **Typical use:** identifying custom fields, extension-vs-new-table classification, and the field-level detail needed to draft the v8 equivalent.

#### `query_schema`

Reads records from a given schema using the generic `ExecuteQuery` method. Same request shape for every schema — only the schema name and selected fields change. Used mainly for config schemas (workflows, deliveries) and for aggregate/count-style validation, not bulk recipient-data extraction.

- **Input:**
  - `schema` *(string, required)* — e.g. `xtk:workflow`, `nms:delivery`.
  - `fields` *(array of strings, required)* — XPath-style field expressions, e.g. `["@internalName", "@label", "@state"]`.
  - `where` *(string, optional)* — XPath-style condition, e.g. `@state = 'active'`.
  - `lineCount` *(number, optional, default 200)* — page size.
  - `startLine` *(number, optional, default 0)* — page offset.
- **Output:** array of record objects for the requested fields.
- **Underlying call:** SOAP `xtk:queryDef#ExecuteQuery` against the named schema.
- **Typical use:** inventorying active vs. dormant workflows, reading delivery configuration, and running count queries for pre- vs. post-migration comparison.

### 6.2 Tool summary

> **⚠️ Superseded by §0.** Phase 1 ships **six** tools, and `get_schema_definition` no longer defaults to WSDL. The authoritative list is below; `plan.md` §4 has full signatures.

| Tool | Input | Output | Underlying call | Primary use |
| --- | --- | --- | --- | --- |
| `test_connection` | none | instance info, latency | `xtk:session#Logon` | Diagnostics |
| `list_schemas` | `namespace?`, `include_builtin?` | JSON schema list | `xtk:queryDef#ExecuteQuery` on `xtk:schema` | Discovery / inventory |
| `get_schema_definition` | `schema`, `form` = `compiled`\|`source`\|`wsdl` | raw schema XML | `xtk:persist#GetEntityIfMoreRecent` (or WSDL GET) | Structural analysis, extension detection |
| `query_schema` | `schema`, `fields`, `where?`, `order_by?`, `page_size?`, `cursor?` | JSON rows | `xtk:queryDef#ExecuteQuery` | Config reads (workflows, deliveries) |
| `count_records` | `schema`, `where?` | `{count}` | `ExecuteQuery` `operation="count"` | Sizing, dead-object detection, reconciliation |
| `get_entity` | `entity_key`, `must_exist?` | raw entity XML | `xtk:persist#GetEntityIfMoreRecent` | Dependency mapping (`@name`-keyed schemas only) |

### 6.3 Resources (Phase 2)

Once schema discovery is cached, each schema is exposed as an MCP **resource** (URI like `acc7://schema/nms:recipient`) so Claude can reference its structure directly without re-querying the instance. This reduces SOAP round-trips, speeds up interactions, and scales better across many schemas. Data is refreshed only when the underlying instance actually changes.

### 6.4 Out of scope (Phase 1)

No write tools are exposed. Any future write capability (drafting schemas/workflows into a **dev** instance) is a later phase, promoted to production only through the standard package export/import process — never a direct production write. See section 9.4 phasing and section 10 for the safety rationale.

---

## 7. Example Implementation (Node.js / TypeScript)

This is illustrative structure, not production-complete. Use a maintained SOAP or HTTP library and the official MCP SDK.

### 7.1 ACC client adapter

```typescript
import axios from "axios";
import { XMLParser } from "fast-xml-parser";

interface AccConfig {
  serverUrl: string;   // https://<server>
  login: string;
  password: string;
}

export class Acc7Client {
  private sessionToken: string | null = null;
  private securityToken: string | null = null;
  private parser = new XMLParser({ ignoreAttributes: false, attributeNamePrefix: "@_" });

  constructor(private cfg: AccConfig) {}

  private async post(soapAction: string, body: string): Promise<any> {
    const res = await axios.post(
      `${this.cfg.serverUrl}/nl/jsp/soaprouter.jsp`,
      body,
      {
        headers: {
          "Content-Type": "text/xml; charset=UTF-8",
          "SOAPAction": soapAction,
        },
        timeout: 30000,
      }
    );
    const parsed = this.parser.parse(res.data);
    this.throwIfFault(parsed);   // see error handling section
    return parsed;
  }

  async logon(): Promise<void> {
    const envelope = buildLogonEnvelope(this.cfg.login, this.cfg.password);
    const parsed = await this.post("xtk:session#Logon", envelope);
    const resp = extractLogonResponse(parsed);
    this.sessionToken = resp.sessionToken;
    this.securityToken = resp.securityToken;
  }

  private async ensureSession(): Promise<void> {
    if (!this.sessionToken) await this.logon();
  }

  async listSchemas(): Promise<Array<{ name: string; namespace: string; label: string }>> {
    await this.ensureSession();
    const envelope = buildQueryEnvelope(this.sessionToken!, "xtk:schema",
      ["@name", "@namespace", "@label"]);
    const parsed = await this.post("xtk:queryDef#ExecuteQuery", envelope);
    return extractSchemaList(parsed);
  }

  async queryData(schema: string, fields: string[], where?: string,
                  page = { lineCount: 200, startLine: 0 }): Promise<any[]> {
    await this.ensureSession();
    const envelope = buildQueryEnvelope(this.sessionToken!, schema, fields, where, page);
    const parsed = await this.post("xtk:queryDef#ExecuteQuery", envelope);
    return extractRecords(parsed, schema);
  }

  async getSchemaWsdl(schema: string): Promise<string> {
    const res = await axios.get(
      `${this.cfg.serverUrl}/nl/jsp/schemawsdl.jsp`,
      { params: { schema }, timeout: 30000 }
    );
    return res.data; // raw WSDL XML
  }
}
```

### 7.2 MCP server wiring

```typescript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Acc7Client } from "./acc7-client.js";

const acc = new Acc7Client({
  serverUrl: process.env.ACC_URL!,
  login: process.env.ACC_LOGIN!,
  password: process.env.ACC_PASSWORD!,
});

const server = new Server(
  { name: "acc7-mcp", version: "1.0.0" },
  { capabilities: { tools: {}, resources: {} } }
);

// --- Tools ---
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "list_schemas",
      description: "List all schemas (tables) in the ACCv7 instance, built-in and custom.",
      inputSchema: { type: "object", properties: {} },
    },
    {
      name: "query_schema",
      description: "Read records from a given schema using ExecuteQuery.",
      inputSchema: {
        type: "object",
        properties: {
          schema: { type: "string", description: "e.g. nms:recipient, xtk:workflow" },
          fields: { type: "array", items: { type: "string" } },
          where:  { type: "string", description: "optional XPath-style condition" },
          lineCount: { type: "number", default: 200 },
          startLine: { type: "number", default: 0 },
        },
        required: ["schema", "fields"],
      },
    },
    {
      name: "get_schema_definition",
      description: "Fetch the WSDL/structure for a schema (fields, types, methods).",
      inputSchema: {
        type: "object",
        properties: { schema: { type: "string" } },
        required: ["schema"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  try {
    switch (name) {
      case "list_schemas":
        return asText(await acc.listSchemas());
      case "query_schema":
        return asText(await acc.queryData(
          args.schema, args.fields, args.where,
          { lineCount: args.lineCount ?? 200, startLine: args.startLine ?? 0 }
        ));
      case "get_schema_definition":
        return asText(await acc.getSchemaWsdl(args.schema));
      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (err) {
    return { isError: true, content: [{ type: "text", text: friendlyError(err) }] };
  }
});

await server.connect(new StdioServerTransport());
```

### 7.3 Exposing schemas as MCP resources (Phase 2)

Rather than re-querying on every request, cache discovered schema definitions and expose each as an MCP resource (URI like `acc7://schema/nms:recipient`). Claude can then reference the structure directly, and you only hit ACC when the underlying data actually needs refreshing. This reduces SOAP round-trips, speeds up interactions, and scales better across many schemas.

---

## 8. Error Handling

SOAP faults come back as `SOAP-ENV:Fault` elements, not HTTP error codes, so a `200 OK` can still contain an error. Always parse the body.

### 8.1 Categories to handle

| Category                   | Signal                                            | Handling                                                            |
| -------------------------- | ------------------------------------------------- | ------------------------------------------------------------------- |
| Auth / session expiry      | Fault code`XSV-350008` or "session has expired" | Re-logon transparently, retry once                                  |
| Bad credentials            | Fault on`Logon`                                 | Fail fast, surface a clear message; do not retry                    |
| Rate limiting / throughput | HTTP`429`                                       | Back off (exponential), respect any`Retry-After`; pace bulk pulls |
| Malformed query            | Fault referencing schema/XPath                    | Return the fault text to Claude so the query can be corrected       |
| Network / timeout          | axios timeout / connection reset                  | Retry with backoff (bounded); then surface                          |
| Permission denied          | Fault indicating access rights                    | Surface clearly — the API account lacks rights on that schema      |

### 8.2 Patterns

- **Parse-then-check:** every response goes through `throwIfFault()` before use.
- **Single transparent re-logon** on session expiry, then propagate if it still fails.
- **Bounded retries with exponential backoff** for transient network/429 errors — never infinite.
- **Never leak tokens** into logs or error messages returned to Claude.
- **Friendly errors to Claude:** translate raw faults into actionable text (e.g. "The account lacks read rights on `cus:loyaltyTier`") so Claude can react sensibly.

### 8.3 Throughput / 429 note

v8's API layer enforces a throughput limit returning `429`, adjustable only by contacting Adobe (Managed Cloud Services). v7 instances can also throttle or time out under heavy pulls. For bulk inventory across a large instance, paginate, pace requests (small delay between pages), and cap concurrency. Assume you may need to slow down mid-run.

---

## 9. Deployment Guidance

### 9.1 Runtime

- Package as a standalone service (Node.js or Python). Runs anywhere — Mac, Linux, container. No Windows dependency; the desktop client is irrelevant to the server.
- **Transport:** stdio for local/desktop Claude use; HTTP/SSE if hosting centrally for a team.
- Containerize (Docker) for consistent deployment and easy handoff.

### 9.2 Network & access prerequisites (what to request from the team)

- **SOAP endpoint URL** (`https://<server>/nl/jsp/soaprouter.jsp`).
- **A technical/API account** with read rights on the schemas in scope.
- **Network reachability** from wherever the server runs (VPN, IP allowlisting). Sandbox/stage instances are often locked down — confirm you can actually reach the endpoint.
- **Instance version** — to know whether native logon or IMS auth applies (v8.5.1+ → IMS).

### 9.3 Secrets & config

- Credentials via environment variables or a secrets manager — never hardcoded, never committed.
- Config: server URL, auth mode (native/IMS), page size, concurrency cap, timeout.

### 9.4 Phasing

- **Phase 1 (read-only):** `list_schemas`, `query_schema`, `get_schema_definition`. Discovery + inventory + dependency data for the migration work. No writes.
- **Phase 2 (resources & caching):** expose schemas as MCP resources, cache definitions, reduce round-trips.
- **Phase 3 (dev writes, later & gated):** optional writes to a **dev** instance only, promoted to production **only** through the standard package export/import process (a human-executed gate), never a direct production write. Workflow execution / sends require separate, explicit human confirmation and sandboxed delivery channels.

---

## 10. Security & Safety Notes

- **Read-only by default.** Phase 1 exposes no write path. This is both a safety choice and aligns with the platform's own package-promotion gate for production changes.
- **Least-privilege API account.** Grant only the read rights needed for the schemas in scope.
- **Token hygiene.** Cache in memory only; never log; rotate credentials per policy.
- **Data sensitivity.** Campaign instances hold real recipient PII. For analysis, prefer counts/metadata and non-PII fields where possible; if PII must be read, handle and store it according to the client's data-handling requirements. Dev/test work should use seed/test profiles, not real recipient data.

---

## 11. Quick Reference

| Operation | Type | Endpoint / Method | Verified |
| --- | --- | --- | --- |
| Authenticate | SOAP | `xtk:session#Logon` → soaprouter.jsp | ✅ |
| List all schemas | SOAP | `xtk:queryDef#ExecuteQuery` on `xtk:schema` | ✅ |
| Read records | SOAP | `xtk:queryDef#ExecuteQuery` on `<schema>` | ✅ |
| Count records | SOAP | `ExecuteQuery` with `operation="count"` | ✅ |
| Compiled schema | SOAP | `xtk:persist#GetEntityIfMoreRecent`, `strPk="xtk:schema\|<name>"` | ✅ |
| Source schema | SOAP | `xtk:persist#GetEntityIfMoreRecent`, `strPk="xtk:srcSchema\|<name>"` | ✅ |
| Schema WSDL | HTTP GET | `schemawsdl.jsp?schema=<schema>` **(session cookie required)** | ✅ |
| Read workflows (metadata) | SOAP | `ExecuteQuery` on `xtk:workflow` | ✅ |
| Read deliveries | SOAP | `ExecuteQuery` on `nms:delivery` | ✅ |
| Read recipients | SOAP | `ExecuteQuery` on `nms:recipient` | ✅ |
| Workflow activity XML | — | no working method found yet — see §0 | ❌ |

**Required headers on every authenticated SOAP call**

```
Content-Type: text/xml; charset=UTF-8
SOAPAction:   <schema>#<Method>
X-Security-Token: <securityToken>
Cookie:       __sessiontoken=<sessionToken>
```

**Endpoints**

- SOAP router: `https://<server>/nl/jsp/soaprouter.jsp`
- WSDL generator: `https://<server>/nl/jsp/schemawsdl.jsp?schema=<schema>`

**Reserved namespaces (built-in, do not use for custom):** `xtk`, `nl`, `nms`, `ncm`, `temp`, `ncl`, `crm`, `xxl`. Custom schemas typically use `cus`.

---

*This document reflects the design discussion for the ACCv7 MCP server. SOAP envelope details, fault codes, and method signatures should be verified against the actual instance's generated WSDLs and Adobe's current API documentation before implementation.*
