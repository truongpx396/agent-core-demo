# Contract: Identity Boundary

**Feature**: [spec.md](../spec.md) | **Related**: [conversation-ownership.md](./conversation-ownership.md), feature 001 [chat-turn-http.md](../../001-core-rag-agent-turn/contracts/chat-turn-http.md)

**Status**: Retrospective — read from `app/api/main.py`, `app/core/security.py`,
`app/channels/chat.py`, `app/channels/telegram.py`, `scripts/seed.py`, `app/mcp/server.py`.

## The contract in one paragraph

Identity is `{tenant, principal, claims}`. It is created **once**, at a trusted boundary, passed to
the graph as `config["configurable"]["ctx"]`, copied into `State.ctx` by the first node of the turn,
and is read-only from there. It is **never** read from message text, request-body fields, tool
arguments or model output. If it is missing or malformed the request is refused; there is no
default tenant and no anonymous mode. This is **authorization/isolation, not authentication**.

## HTTP boundary

| Header | Required | Missing / empty | Notes |
|--------|----------|-----------------|-------|
| `X-Tenant-Id` | yes | **422**, handler never runs | opaque string |
| `X-Principal-Id` | yes | **422**, handler never runs | opaque string; owner of memories and conversations |
| `X-Domain` | no (default `ecorp`) | default applies | unknown value ⇒ **422** `Unknown X-Domain '<x>' — must be one of: …`; validated only as *existing* — **not** authorized per tenant |

Guarantees: no request-body field can set identity; there is no default identity; a client-supplied
copy of the headers is only safe if the deployment's gateway strips and re-sets them.
**Non-guarantee**: nothing verifies the header values. The shipped production proxy neither
authenticates callers nor sets/strips these headers (research R17); the demo web UI exposes both as
editable fields.

## Other boundaries

| Boundary | Tenant | Principal | Fail-closed behavior |
|----------|--------|-----------|----------------------|
| CLI `make chat` | `DEFAULT_TENANT` (`ecorp`) | `local:<OS user>` | stamped constant `_LOCAL_CTX` |
| Telegram | `DEFAULT_TENANT` | `telegram:<user_id>` | one principal per Telegram user, one shared tenant |
| Seeding `make ingest` | `DEFAULT_TENANT` | `make-ingest` | ingestion requires a valid ctx; content is never ingested tenant-less |
| MCP server (`make mcp-serve`, stdio) | tool argument | tool argument | `DEFAULT_POLICY.permit("query_structured_data", ctx)` ⇒ `"Refused: tenant and principal are required."`; caller **not** authenticated (documented demo simplification) |

`DEFAULT_TENANT` is a *stamped identity for local boundaries*, not a fallback for a missing one.

## Refusal shapes (what a caller observes)

| Situation | Where | Observable |
|-----------|-------|-----------|
| Missing/empty header | HTTP dependency | `422` |
| Valid HTTP, ctx later invalid (should not occur via HTTP) | `route_after_validation` → `reject_context` | assistant text "I couldn't verify who's asking, so I can't help with this request. Please try again." then `done`; `agent_missing_ctx_total` +1 |
| Tool invoked without valid ctx | `_ctx_or_refuse` → `_NO_CTX_REFUSAL` | `ToolMessage` "Refused: no valid tenant/principal context for this request. This isn't something you can work around — …" (a normal message, not an exception) |
| Policy denies action | `DEFAULT_POLICY.permit` → `False` | same refusal as above |
| Ingestion without ctx | `IngestRefused` | refused and counted by reason (`agent_ingest_refused_total{reason}`) |
| Memory deletion without ctx | `delete_memories` | `ValueError("a valid ctx is required to delete memories")`; `agent_memory_deletion_total{outcome="refused"}` +1 |

## Policy actions (default-deny vocabulary)

`search` · `write_note` · `recall_memory` · `write_memory` · `query_structured_data`. Anything else
is denied. Domains add their own vocabularies through `ActionAllowlistPolicy` (e.g. `create_ticket`,
`schedule_followup`).

## Invariants a change must preserve

1. `State.ctx` is written by exactly one node (`validate_input`).
2. No `@tool` `args_schema` contains `tenant` or `principal` (the MCP tool is the single exception
   and lives outside the agent).
3. Every ctx-aware tool calls `_ctx_or_refuse` first and returns the refusal instead of raising.
4. Unknown ⇒ deny. A new policy action must be added to `_KNOWN_ACTIONS` deliberately.
5. `SYSTEM_PROMPT` stays ctx-free (feature 001): identity flows through `config`/`State`, never the
   prompt.
