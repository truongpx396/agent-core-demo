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
- **Each wrapped tool**: its own name; its own description; **its own raw JSON schema as the argument schema**, so the model sees real parameter names. *(With `pinned`, only the tools the caller named, and only while a hash of name + description + schema still matches what was pinned; anything else is dropped, a changed or missing one is counted. Without it, the remote's text passes through as given: trust on first use.)*
- **Each call**: uses the **one long-lived connection** to that server on the running event loop (`app/mcp/connection.py`), which starts on first use and is replaced when found dead. A call that never left is sent once more; one that was in flight when the server died is **not** replayed (`ConnectionError`). The reply is rendered (text joined with a newline; an embedded text resource and a link shown; an image or audio block becomes a one-line note; with no text, `structuredContent` as JSON), **credential-scrubbed**, and then cut to `MCP_TOOL_OUTPUT_MAX_CHARS` keeping head and tail with a marker (unless the caller passed `cap_output=False` because it parses the reply and bounds it itself); if the remote flagged an error the result is `Remote tool error: <text>` (returned, not raised).
- **Child process environment**: `env=None` → the SDK's restricted default environment, not the app's.
- **The sandbox bridge's API key is passed in the child's environment** (`OPEN_SANDBOX_API_KEY`), not on its command line; the SDK merges it into its small default child environment.

## `ecorp-structured-data` — `app/mcp/server.py` (`make mcp-serve`)

| Part | Contract |
|------|----------|
| Tool | `query_employees(department?, name_contains?)`, annotated read-only and closed-world |
| Identity | the **launcher's**: `MCP_TENANT` and `MCP_PRINCIPAL` in the process's environment, read once into a `SecurityCtx` and checked against the fail-closed policy gate (`DEFAULT_POLICY.permit`). No tool takes either as an argument; a server started without them exits before serving, and a tool called without one → `Refused: this server was started without an identity …` |
| Query | a fixed parameterized statement with a mandatory `WHERE tenant = %s`; `department` is a closed enum (`Engineering`, `Support`, `Sales`) — an invalid value returns the valid list; there is no free-form query |
| Audit | one `mcp_tool_call` JSON line on stderr per call: server, tool, tenant, principal, outcome, duration; never an argument or a result |
| Not done | **authenticating whoever starts the process** (A4): over stdio that is the machine's owner |

## `ecorp-ops` — `app/mcp/ops_server.py` (`make mcp-serve-ops`)

| Part | Contract |
|------|----------|
| Tools | `fetch_metrics_summary(principal)`, `list_recent_incidents(principal, status?)` — both read-only |
| Identity | `principal` is an argument; the tenant is fixed to the default (ops data is not tenant-scoped); a missing principal → `Refused: principal is required.` |
| Process | a **separate** process from the directory server — one process per domain, like the workers |

## Running a published server

- Standard input and output only (`mcp.run(transport="stdio")`); the caller is the person (or program) that starts the process. **A networked transport would make both servers unauthenticated multi-tenant APIs (A4).**
- The servers are built on `mcp.server.MCPServer` (`mcp[cli]==2.3.0`). The sandbox bridge, `opensandbox-mcp` 1.1.1, requires mcp 2.x too; releases up to 0.1.1 imported `mcp.server.fastmcp`, which 2.x removed, and had held the app on 1.x (`tests/core/test_mcp_sdk_pin.py`).

## Failure behavior

| Situation | Result |
|-----------|--------|
| The remote reports an error | `Remote tool error: <scrubbed text>` as the tool's string result |
| The server cannot start (e.g. the bridge script is missing — B23) | the loader raises; `load_sandbox_tools` catches it, logs `opensandbox_mcp_unavailable`, returns `([], {})` |
| A published server called without an identity | `Refused: …`, no data touched |

## Invariants a change must preserve

1. A remote tool's capability never comes from the remote.
2. Every remote reply is scrubbed before it reaches the model or a trace.
3. A published server refuses a missing identity **before** touching data and runs only fixed, parameterized queries, and takes its identity from its launcher, never from a tool argument.
   (Added with the MCP-hardening change:) a call that may have reached a remote server is never sent a second time, and a remote reply is scrubbed **before** it is cut.
4. The directory and operations servers stay separate processes.
5. **Before any networked transport exists, identity comes from the connecting program's verified credentials** *(not yet built — A4)*.
