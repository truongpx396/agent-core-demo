# Contract: Conversation Ownership

**Feature**: [spec.md](../spec.md) | **Finding**: [research.md R15](../research.md) | **Identity**: [identity-boundary.md](./identity-boundary.md)

**Status**: Retrospective. This contract states **what the code does on each endpoint** (the
*as-built* column) next to **what the spec requires** (FR-012), because they differ. The as-built
column was established by grepping for the ownership check (`session_belongs_to`) and reading each
handler; the graph-level consequence was reproduced with a fake model and an in-memory store; the
HTTP consequence was **not** reproduced against a running stack.

## Ownership key

A conversation is owned by `(tenant, principal, domain)` recorded in `chat_sessions` the first time
a turn starts on its `thread_id`. The conversation's *state* (checkpoint), its per-thread lock
(`agent:lock:<thread_id>`) and its cancel flag (`agent:cancel:<thread_id>`) are keyed by `thread_id`
alone and carry no owner.

## Endpoint matrix

| Endpoint | Touches | Required (FR-012) | **As built** | Behavior when not the owner |
|----------|---------|-------------------|--------------|-----------------------------|
| `GET /chat/sessions` | the caller's list | scope to `(tenant, principal, domain)` | **scoped** | only own rows appear |
| `GET /chat/sessions/{thread_id}/messages` | transcript | owner check | **checked** (`session_belongs_to`) | `404 {"detail":"session not found"}` — identical for "not yours" and "does not exist" |
| `GET /chat/sessions/{thread_id}/pending_approval` | paused tool calls | owner check | **checked** | same `404` |
| `POST /chat/stream/queued` | appends a turn to the checkpoint | owner check, with claim-on-first-use | **NOT checked** | the turn runs on that conversation's history under the caller's ctx |
| `POST /chat/resume` | approves/rejects a paused call | owner check | **NOT checked** | the call runs (or not) under the **resumer's** ctx |
| `POST /chat/cancel` | sets the cancel flag; cancels a paused run | owner check | **NOT checked** | the flag is keyed by `thread_id` alone |
| worker job kinds `turn`, `turn_continue`, `resume`, `cancel` | checkpoint | verify before running | **NOT checked** | — |
| `GET /usage` | caller's tenant spend | tenant scope | **scoped** | n/a |
| `POST /ingest/upload` | tenant's corpus | valid ctx | **ctx required** | n/a |
| `GET /ingest/stream/{job_id}` | job progress | none (server-generated id) | **unchecked by design** | relies on an unguessable `uuid4().hex` |

Response bodies for the checked reads: `SessionMessage {role: "user"|"assistant"|"system", text}`,
`PendingApproval {tool_calls: [...], resumable: bool}` (feature 001/003 contracts).

## Observed consequence (graph level, reproduced)

Tenant A runs a turn on `thread_id = T`. Tenant B (different tenant *and* principal) then runs a turn
on the same `T` with its own ctx. Result: the history B's turn ran on contained A's earlier message,
and the stored `ctx` became B's. The checkpoint is not partitioned by caller.

## Id predictability

| Producer | `thread_id` | Guessable? |
|----------|-------------|------------|
| Web UI / API default | `uuid4()` (client-side) | no |
| Any API client | any string (e.g. `qs-1`, `alice-chat`) | yes, by choice |
| Telegram | `telegram:<chat_id>` | yes — small integer |
| CLI | per-session value | local only |

## What a fix must satisfy (acceptance criteria for the open task)

1. A caller who is not the owner of `T` cannot send to, resume, cancel, or have a turn continue on
   `T`, **including via the worker** — a failing two-tenant test is written first.
2. A **new** `thread_id` must work (claim-on-first-use) and two simultaneous first uses must resolve
   to exactly one owner.
3. The non-owner response must not reveal whether `T` exists (match the `404` used by the reads).
4. Existing conversations keep working (migration or read-through for legacy ids).
5. Telegram's derived ids keep resolving to the same conversation.
6. The cancel flag and thread lock are not usable as a side channel to disturb another's run.

Candidate designs (namespace the checkpoint key vs. check on every endpoint vs. worker-side
verification) are compared in research R15.
