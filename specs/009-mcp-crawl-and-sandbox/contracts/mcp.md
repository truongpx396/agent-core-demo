# Contract: The MCP Client and the Two Published Servers

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §3, §8](../data-model.md) | **Constitution**: Principle I (identity at the boundary), Principle II (capability fail-closed), Principle VI (scrubbing)

**Status**: Retrospective — `app/mcp/client.py`, `app/mcp/server.py`, `app/mcp/ops_server.py`, `app/domains/sandbox_tools.py` (the one caller of the client); tests in `tests/mcp/test_mcp_client.py`, `test_mcp_server.py`, `test_ops_server.py`.

## Audience

An **integrator** loading another program's tools, a **person** running a published server from a desktop assistant, and a **reviewer** checking the trust boundary.

## The client — `load_remote_tools(command, args, capability_overrides, env, cwd)`

- **Transport**: standard input and output; a child process started with `command` and `args`.
- **Returns** `(langchain_tools, tool_capabilities)`; the second merges into a product's capability table.
- **Capability** of each remote tool: `capability_overrides.get(name, "outward")`. The override map comes **only from the local caller**; the remote server's own annotations (`readOnlyHint` …) are **never read**. A tool the caller did not name is therefore `outward` and pauses for approval.
  The sandbox bridge is loaded with an **empty** map, so all ~19 of its tools are `outward`.
- **Each wrapped tool**: its own name; its own description; **its own raw JSON schema as the argument schema**, so the model sees real parameter names. *(The description and schema are the remote's text and pass through as given — A5.)*
- **Each call**: opens a **fresh** connection, initializes, calls, closes — no persistent session. The reply's text parts are joined and **credential-scrubbed**; if the remote flagged an error the result is `Remote tool error: <text>` (returned, not raised). **No size cap (A3).**
- **Child process environment**: `env=None` → the SDK's restricted default environment, not the app's.
- **The sandbox bridge's API key is passed as `--api-key <value>` on the child's command line** — visible in a process listing (A5).

## `ecorp-structured-data` — `app/mcp/server.py` (`make mcp-serve`)

| Part | Contract |
|------|----------|
| Tool | `query_employees(tenant, principal, department?, name_contains?)` |
| Identity | `tenant` and `principal` are **caller-supplied arguments**, checked against the fail-closed policy gate (`DEFAULT_POLICY.permit`); missing either → `Refused: tenant and principal are required.` |
| Query | a fixed parameterized statement with a mandatory `WHERE tenant = %s`; `department` is a closed enum (`Engineering`, `Support`, `Sales`) — an invalid value returns the valid list; there is no free-form query |
| Not done | **authenticating the caller** (A4): any program that can start the server can name any tenant |

## `ecorp-ops` — `app/mcp/ops_server.py` (`make mcp-serve-ops`)

| Part | Contract |
|------|----------|
| Tools | `fetch_metrics_summary(principal)`, `list_recent_incidents(principal, status?)` — both read-only |
| Identity | `principal` is an argument; the tenant is fixed to the default (ops data is not tenant-scoped); a missing principal → `Refused: principal is required.` |
| Process | a **separate** process from the directory server — one process per domain, like the workers |

## Running a published server

- Standard input and output only (`mcp.run(transport="stdio")`); the caller is the person (or program) that starts the process. **A networked transport would make both servers unauthenticated multi-tenant APIs (A4).**
- `mcp[cli]` is pinned to 1.29.0 (2.0 removed the high-level server API).

## Failure behavior

| Situation | Result |
|-----------|--------|
| The remote reports an error | `Remote tool error: <scrubbed text>` as the tool's string result |
| The server cannot start (e.g. the bridge script is missing — B23) | the loader raises; `load_sandbox_tools` catches it, logs `opensandbox_mcp_unavailable`, returns `([], {})` |
| A published server called without an identity | `Refused: …`, no data touched |

## Invariants a change must preserve

1. A remote tool's capability never comes from the remote.
2. Every remote reply is scrubbed before it reaches the model or a trace.
3. A published server refuses a missing identity **before** touching data and runs only fixed, parameterized queries.
4. The directory and operations servers stay separate processes.
5. **Before any networked transport exists, identity comes from the connecting program's verified credentials** *(not yet built — A4)*.
