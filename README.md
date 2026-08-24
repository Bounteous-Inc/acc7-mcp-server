# ACCv7 MCP Server

Read-only MCP server for Adobe Campaign Classic v7, built for migration
inventory, schema audit and dependency mapping.

**Status:** in progress. All six tools are registered and discoverable.
Implemented and verified against a live v7.4.3 instance: `test_connection`,
`list_schemas`, `count_records`. The remaining three
(`get_schema_definition`, `query_schema`, `get_entity`) raise
`NotImplementedError`.

See `plan.md` for the implementation plan, `my-ref.md` for the quick reference,
and `acc7-mcp-server-plan.md` §0 for verified errata against the original design.

The server is **generic**: it works against any ACCv7 instance. `MCP_TRANSPORT`
picks the transport, and that determines how a client says which instance it
wants:

| `MCP_TRANSPORT`  | Tenancy                           | Connection details from     | Use for           |
| ------------------ | --------------------------------- | --------------------------- | ----------------- |
| `stdio`          | single — one process per client  | environment variables       | local development |
| `http` (default) | multi — many clients per process | `X-ACC-*` request headers | shared deployment |

Either way credentials never pass through tool arguments, so they never enter
the model's context.

## Setup

```bash
uv venv
uv pip install -e .
cp .env.example .env      # optional — server-level tuning only
```

## Connect to Claude Code

### stdio — simplest for local work

Claude starts and stops the server itself; nothing to run beforehand.

```bash
claude mcp add acc7 -s project \
  -e MCP_TRANSPORT=stdio \
  -e ACC_BASE_URL=https://<instance-host> \
  -e ACC_LOGIN=<native operator login> \
  -e ACC_PASSWORD=<operator password> \
  -- /abs/path/to/.venv/bin/acc-mcp-server
```

### http — the deployment path

Start the server first; it keeps running and serves many clients.

```bash
MCP_TRANSPORT=http .venv/bin/acc-mcp-server    # http://127.0.0.1:8000/mcp
```

```bash
claude mcp add --transport http acc7 http://127.0.0.1:8000/mcp -s project \
  --header "X-ACC-Base-Url: https://<instance-host>" \
  --header "X-ACC-Login: <native operator login>" \
  --header "X-ACC-Password: <operator password>"
```

Host and port come from `MCP_HTTP_HOST` / `MCP_HTTP_PORT`. `MCP_TRANSPORT=https`
is accepted as an alias for `http` — the process always serves plain HTTP, with
TLS terminated by whatever proxy sits in front of it.

**Under http the server must already be running.** If it is not, the tools
simply will not appear — which reads like a broken config rather than a stopped
process.

Either way, `/mcp` in a Claude session confirms the six tools, and
`test_connection` confirms the instance answers.

| Header              | Required | Meaning                                 |
| ------------------- | -------- | --------------------------------------- |
| `X-ACC-Base-Url`  | yes      | Scheme + host only, no`/nl/jsp` path  |
| `X-ACC-Login`     | yes      | Native operator login (not an Adobe ID) |
| `X-ACC-Password`  | yes      | That operator's password                |
| `X-ACC-Auth-Mode` | no       | `native` (default)                    |

Each distinct instance+operator gets its own client and its own session, so
tenants never share tokens. Clients are cached (LRU, `ACC_MAX_CACHED_CONNECTIONS`)
so repeat calls reuse a single logon.

Under `http`, `ACC_ALLOW_ENV_FALLBACK=true` lets the `ACC_*` env vars stand in
for missing headers. It is off by default so a shared deployment can never
serve a stale instance to a client that forgot them. It has no effect under
`stdio`, where the environment is already the only channel.

## Error contract

Tools return failures as data, never as bare strings, so an agent can decide
what to do without guessing:

```json
{
  "isError": true,
  "isRetryable": false,
  "errorType": "AuthError",
  "message": "Authentication failed: the instance rejected these credentials...",
  "retryGuidance": "Do not retry. The credentials are wrong or the account..."
}
```

Retryable errors also carry `retryAfterSeconds`. Successful results carry
`"isError": false`. Protocol-level `isError` is reserved for unhandled crashes.

## Tools

| Tool                      | Purpose                                                        |
| ------------------------- | -------------------------------------------------------------- |
| `test_connection`       | Instance reachable + credentials valid                         |
| `list_schemas`          | Discovery — every schema, built-in and custom                 |
| `get_schema_definition` | Structure of one schema (`source` / `compiled` / `wsdl`) |
| `query_schema`          | Read records from any schema                                   |
| `count_records`         | Row count without transferring rows                            |
| `get_entity`            | Full entity XML — dependency edges                            |

Authentication is server-internal: `xtk:session#Logon` fires lazily before the
first ACC call. There is no `logon` tool, and tokens are held in memory only.

## Layout

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
