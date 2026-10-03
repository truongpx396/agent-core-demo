# Contract: Conversation Ownership

**Feature**: [spec.md](../spec.md) | **Finding**: [research.md R15](../research.md) | **Identity**: [identity-boundary.md](./identity-boundary.md)

**Status**: Retrospective, **updated 2026-10-03 after #67**. This contract states what the code does on each endpoint next to what the spec requires (FR-012). It was first written when send, resume and cancel were unchecked (bug B2); #67 closed that, and the *as-built* column now reflects it. The original graph-level reproduction is kept below as history; the HTTP behavior is covered by `tests/api/test_api.py::TestConversationOwnership` and, for the claim itself, a real-database test.

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
| `POST /chat/stream/queued` | appends a turn to the checkpoint | owner check, with claim-on-first-use | **checked and claimed** (`claim_session`, first) | `404 session not found`; nothing enqueued; the submission-dedup stream is not handed over |
| `POST /chat/resume` | approves/rejects a paused call | owner check | **checked** (`session_belongs_to`, no claim) | same `404`; nothing enqueued; a thread nobody owns is refused, not run |
| `POST /chat/cancel` | sets the cancel flag; cancels a paused run | owner check | **checked, before the flag is set** | same `404`; **no flag is written** for a non-owner |
| worker job kinds `turn`, `turn_continue`, `resume`, `cancel` | checkpoint | verify before running | **NOT checked** (the API is the boundary; defence in depth is task T043) | — |
| `GET /usage` | caller's tenant spend | tenant scope | **scoped** | n/a |
| `POST /ingest/upload` | tenant's corpus | valid ctx | **ctx required** | n/a |
| `GET /ingest/stream/{job_id}` | job progress | none (server-generated id) | **unchecked by design** | relies on an unguessable `uuid4().hex` |

Response bodies for the checked reads: `SessionMessage {role: "user"|"assistant"|"system", text}`,
`PendingApproval {tool_calls: [...], resumable: bool}` (feature 001/003 contracts).

## Observed consequence before #67 (graph level, reproduced — kept as history)

Tenant A runs a turn on `thread_id = T`. Tenant B (different tenant *and* principal) then runs a turn
on the same `T` with its own ctx. Result: the history B's turn ran on contained A's earlier message,
and the stored `ctx` became B's. The checkpoint is not partitioned by caller.

## Id predictability

| Producer | `thread_id` | Guessable? |
|----------|-------------|------------|
| Web UI / API default | `uuid4()` (client-side) | no |
| Any API client | any string (e.g. `qs-1`, `alice-chat`) | yes, by choice |
| Telegram | `telegram:<chat_id>` | yes — small integer; **never claimable over HTTP** (`sessions.TELEGRAM_THREAD_PREFIX`), though its owner may continue it |
| CLI | per-session value | local only |

## What the fix had to satisfy (acceptance criteria) and how #67 meets them

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

### Status of each criterion after #67

1. Non-owners cannot send, resume or cancel — **met at the API** (`TestConversationOwnership`); **not repeated in the worker** (open, T043).
2. A new id works and a race has one winner — **met** (`claim_session`; the real-database race test).
3. The non-owner response does not reveal existence — **met** (the same 404 as the reads, for "someone else's", "nobody's" and, on resume, "unowned").
4. Existing conversations keep working — **met** for any conversation with a session row (every turn writes one at its start); one without a row cannot be resumed or cancelled.
5. Telegram's derived ids keep resolving — **met**; the prefix is reserved against claiming over HTTP.
6. The cancel flag and thread lock are not a side channel — **met for the flag** (checked before it is written); the lock is only taken by a worker running an already-authorized job.
