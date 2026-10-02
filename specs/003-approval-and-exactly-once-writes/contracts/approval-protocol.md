# Contract: Approval Protocol

**Feature**: [spec.md](../spec.md) | **Events**: feature 001 [turn-event-stream.md](../../001-core-rag-agent-turn/contracts/turn-event-stream.md) | **Errors**: feature 001 [error-envelope.md](../../001-core-rag-agent-turn/contracts/error-envelope.md) | **Ownership**: feature 002 [conversation-ownership.md](../../002-tenant-isolation-and-memory/contracts/conversation-ownership.md)

**Status**: Retrospective — `app/agent/graph_hitl.py`, `app/agent/runtime_stream.py`,
`app/api/main.py`, `app/api/schemas.py`, `app/job_queue/agent_worker.py`. Where the behavior departs
from the spec's requirements it is marked **[defect]** with its id.

## 1. When a turn pauses

A model round requests tool calls; `should_continue` pauses the turn if **any** call is not
`read_only` (undeclared ⇒ `outward`), or the caller set `require_approval`. The turn's stream ends with
exactly one terminal event:

```json
{ "type": "approval_required", "tool_calls": [ { "name": "create_ticket", "args": { "...": "..." } } ] }
```

- `args` are what the model proposed; **the call id is not included**.
- Nothing has run. The checkpoint holds the pause durably (survives restarts).
- A batch with an unknown tool name, more than 5 calls, or a `use_skill` without a prior `skill_search`
  never reaches this point: it is bounced to the model.

## 2. The three decisions

| Decision | Wire form | Effect | Model sees | Next |
|----------|-----------|--------|-----------|------|
| **approve** | `approved: true` | every pending call runs | its real results | `agent` |
| **reject** | `approved: false` | none run | one `ToolMessage` per call: *"Rejected by human reviewer."* | `agent` (may answer or re-propose) |
| **cancel** | `POST /chat/cancel` (resume value `"cancelled"`) | none run | one `ToolMessage` per call: *"Cancelled by request — this action will not run."* | **`__end__`** — the model is never called again |

Metric: `agent_human_approval_total{decision ∈ approved|rejected|cancelled}`. Capability gating:
`agent_capability_gate_total{capability ∈ mutating|outward}`.

## 3. Endpoints

All require `X-Tenant-Id` / `X-Principal-Id` (422 if absent) and accept `X-Domain` (default `ecorp`; must match
the original turn's domain). All respond with the SSE vocabulary of feature 001.

### `POST /chat/resume` — approve or reject

Request: `{"thread_id": string, "approved": bool}`. Rate-limited (30/min per tenant). Published as a `resume`
job; any worker in the domain's pool may handle it. **Identity is re-supplied by this request** — the pause does
not remember it; the pending action runs under *this* request's identity. **[feature 002 B2 — fixed in #67]**: the API now verifies the caller owns `thread_id` first
(`404 session not found` otherwise, before anything is enqueued); the worker does not repeat the check.

| Situation | Stream |
|-----------|--------|
| paused, schema compatible | the resumed turn's events, then `done` (or a further `approval_required`) |
| not paused (completed / unknown / lost) | `error` with `content` beginning `checkpoint_lost: …`, then `done`; `agent_checkpoint_issue_total{reason="checkpoint_lost"}` +1. **No `code` field** (feature 001 envelope deviation 2) |
| paused under a different `STATE_SCHEMA_VERSION` | `error` beginning `checkpoint_incompatible: …`, then `done`; counted likewise |
| another job is running on the conversation (including a turn still streaming) | `error{code:"thread_busy"}` — the conversation lock is checked first; if the lock had already expired, a still-running checkpoint is *not* treated as paused (it reads as `checkpoint_lost`) rather than starting a competing execution |

A *difference in build identity alone* is **not** an incompatibility.

### `POST /chat/cancel` — stop

Request: `{"thread_id": string}`. **Never rate-limited.** Fires two mechanisms unconditionally, each a no-op if
it does not apply: (1) sets `agent:cancel:<thread_id>` (60 s) — honored by a *new or continued streaming turn* at
its next event boundary, ending it with `error{code:"cancelled", message:"Cancelled by user."}`; (2) publishes a
`cancel` job — if the conversation is paused, resumes it with the cancel sentinel and the job's own stream ends
`error{code:"cancelled"}`; if nothing is paused it ends `done`.

A turn streaming **after an approval** is now cancellable too (fixed in #65): the resume job wires the same cancel check and clears a stale flag, so (1) is read from the first event. A running tool is never interrupted by (1) in any case.

### `GET /chat/sessions/{thread_id}/pending_approval`

Returns `null` (not paused) or `{"tool_calls": [{name, args}…], "resumable": bool}`; `resumable` is false when the
stored schema version differs from the running build (approve/reject would be refused — show it as unresumable).
Owner-checked: another caller's id ⇒ **404** (feature 002).

## 4. A new message while paused

`POST /chat/stream/queued` on a paused conversation:

| Pause is… | Result |
|-----------|--------|
| resumable | `error{code:"pending_approval", message:"This conversation has a pending approval — approve, reject, or cancel it before sending a new message.", details:{tool_calls:[…]}}` — **never auto-cancelled, never discarded** |
| not resumable (incompatible) | a `system_note` ("A previous pending approval … could not be resumed after an app update; starting a new request."), then the new turn proceeds |

## 5. Unattended callers

A caller with no human uses `astream_events_turn_unattended`: on each `approval_required` it resumes with
`approved:false` and counts `agent_unattended_pause_total`; it never approves. The decline is a **loop bounded by `UNATTENDED_MAX_DECLINE_ROUNDS`**
(default 3); if the model is still re-requesting at the ceiling the run is **cancelled** and one explicit message says the action needs a
person's approval and was not done — the thread is never left paused (fixed in #63; formerly defect B4).

## 6. The `cancelled` marker *(defect B3 — fixed in #62)*

`human_approval` sets `State.cancelled = True` on a cancel. `validate_input` now resets it (and `approved`) at the start of every new turn — **not** on a resume. Historically nothing reset it: `route_after_approval` tests it
**first**:

```text
cancelled  → "__end__"        # even if this later pause was APPROVED
approved   → "tools"
otherwise  → "agent"
```

Observed before #62 (reproduced): after a cancel on turn 1, an **approved** pause on turn 2 of the same thread ends the run
with no tool result; the last message is the assistant's own tool request. **Required behavior** (spec SC-010): a
cancel affects only the run it cancelled. Fix (done): reset `cancelled` per turn, **not** on a resume.

## 7. Invariants a change must preserve

1. No path runs a non-`read_only` call without passing `human_approval` (no flag, env var or per-domain
   setting). `require_approval` only *adds* pauses.
2. Reject and cancel each produce **exactly one `ToolMessage` per pending call**.
3. Cancel never returns control to the model.
4. "Paused" means `state.next` **and** a pending interrupt — never `state.next` alone.
5. A resume re-supplies identity; a new message to a resumable pause is refused, not auto-cancelled.
6. Unattended callers decline; they never approve, and never leave a thread they cannot resolve.
