# Data Model: Mandatory Approval and Exactly-Once Writes

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

**Status**: Retrospective — read from the code, SQL and queue module.

## 1. Tool capability

`ToolCapability = Literal["read_only", "mutating", "outward"]`.

| Tier | Meaning | Gate |
|------|---------|------|
| `read_only` | cannot change state or reach outside the corpus | none (runs immediately); `require_approval` can add one |
| `mutating` | writes persisted state | **mandatory** approval |
| `outward` | reaches outside the corpus (sends, fetches, runs code) | **mandatory** approval |
| *undeclared* | name absent from the mapping | treated as `outward` (`_tool_capability`) |

A pending **batch** is gated if *any* call in it is non-`read_only`; the metric label
(`agent_capability_gate_total{capability}`) is the more severe tier present (`outward` > `mutating`).

### 1.1 Declared tools (as built)

| Tool | Tier | Declared in |
|------|------|-------------|
| `search_docs`, `calculator`, `query_employees`, `ask_clarification`, `skill_search`, `use_skill`, `run_subagent` | `read_only` | `app/agent/tools.py` |
| `add_note`, `remember` | `mutating` | `app/agent/tools.py` |
| `check_ticket_status`, `list_my_tickets` | `read_only` | `app/domains/support/tools.py` |
| `create_ticket`, `escalate_to_human`, `add_ticket_comment` | `mutating` | support |
| `fetch_external_reference` | `outward` | support |
| `fetch_metrics_summary`, `list_recent_incidents` | `read_only` | `app/domains/ops/tools.py` |
| `log_incident`, `resolve_incident` | `mutating` | ops |
| `post_to_team_channel`, `check_vendor_status_page` | `outward` | ops |
| `package_lead_brief`, `list_pending_followups` | `read_only` | `app/domains/sales/tools.py` |
| `log_lead_interaction`, `schedule_followup`, `handoff_to_human`, `mark_lead_lost` | `mutating` | sales |
| `enrich_lead_from_website` | `outward` | sales |
| `run_command_in_sandbox`, `run_python_in_sandbox`, `read_sandbox_file`, `write_sandbox_file` | `outward` (capped; OpenSandbox's own annotations are ignored — `capability_overrides={}`) | `app/domains/sandbox_tools.py`, exposed by ops, sales, support |

## 2. Exactly-once, layer by layer (per tool)

Layer 1 = `idempotent(tool_call_id, …)` (`tool_call_dedup`). Layer 2 = idempotence **at the target**.
"Residual" = what remains if layer 1 is bypassed (store down, or the narrow `result IS NULL` race).

| Tool | Layer 1 | Layer 2 (target) | Residual if layer 1 is bypassed |
|------|---------|------------------|---------------------------------|
| `add_note` | ✔ | Qdrant point id `uuid5(ns, tool_call_id)` ⇒ upsert onto the same point | none |
| `remember` | ✔ | same | none |
| `create_ticket` | ✔ | `support_tickets.tool_call_id UNIQUE` + `ON CONFLICT (tool_call_id) DO NOTHING RETURNING id`, read back on conflict | none |
| `add_ticket_comment` | ✔ | `support_ticket_comments.tool_call_id UNIQUE` row (`ON CONFLICT DO NOTHING`); parent `updated_at` bump | none |
| `escalate_to_human` | ✔ | `UPDATE support_tickets SET status='escalated', escalation_reason=…` — naturally idempotent end state | **duplicate team-channel message** (R1) |
| `log_incident` | ✔ | `ops_incidents.tool_call_id UNIQUE` | none |
| `resolve_incident` | ✔ | `UPDATE … SET status='resolved', resolution=…, resolved_at=now()` — idempotent end state; `resolved_at` is refreshed on a replay | timestamp drift only |
| `log_lead_interaction` | ✔ | lead upsert `ON CONFLICT (tenant, contact) DO UPDATE SET updated_at` (naturally idempotent) + note row `crm_lead_notes.tool_call_id UNIQUE` | none |
| `schedule_followup` | ✔ | `crm_followups.tool_call_id UNIQUE` | none |
| `handoff_to_human` | ✔ | `UPDATE crm_leads SET status='handed_off'` — idempotent end state | **duplicate team-channel message** (R1) |
| `mark_lead_lost` | ✔ | status `UPDATE` (idempotent) + reason note row (`UNIQUE`) + cancel follow-ups (`UPDATE`, idempotent) | none |
| `post_to_team_channel` (ops) | ✔ | none — the send *is* the effect | **duplicate message** (R1) |
| `check_vendor_status_page`, `enrich_lead_from_website`, `fetch_external_reference` | ✔ | none — outward reads (idempotent by nature; a repeat costs time) | repeated external fetch |
| 4 sandbox tools | ✔ | none — a command/script/file write inside an ephemeral sandbox | repeated execution |

Store functions take `tool_call_id: str | None = None`; omitting it yields **no** row-level protection.

## 3. `tool_call_dedup` (Postgres `appdata`, `postgres-init/13-tool-call-dedup.sql`)

| Column | Type | Constraint | Role |
|--------|------|-----------|------|
| `tool_call_id` | TEXT | **PRIMARY KEY** | the atomic claim and the lookup key (**not** tenant-scoped — E2) |
| `tenant` | TEXT | NOT NULL | observability metadata only |
| `thread_id` | TEXT | nullable | observability metadata; index `tool_call_dedup_thread_id_idx` |
| `tool_name` | TEXT | NOT NULL | observability metadata |
| `result` | TEXT | nullable | `NULL` while in flight; set once after `fn()` returns; the text replayed to a later caller |
| `created_at` | TIMESTAMPTZ | NOT NULL DEFAULT `now()` | retention |

### 3.1 Claim lifecycle

```text
 (absent) ──INSERT … ON CONFLICT DO NOTHING──► claimed (result IS NULL) ──fn() ok, UPDATE result──► completed
     │                                              │                                                   │
     │                                              ├─ fn() raises TimeoutError ─► MutatingToolTimedOut  │
     │                                              │   (row stays result IS NULL; agent told to verify) │
     │                                              └─ a 2nd caller reads NULL ─► runs fn() itself       │
     │                                                  (accepted narrow race; target layer 2 covers it) │
     └─ store unreachable ─► run fn() UNPROTECTED, agent_tool_dedup_degraded_total +1                    │
                                                                       sweep_stale_rows(older_than_hours) ◄┘
```

A caller that finds `result` set returns it **unchanged** (possibly stale vs. the target's current state).

## 4. Target-level columns (`postgres-init/14-…`, `15-…`)

| Table | Column / structure | Notes |
|-------|--------------------|-------|
| `support_tickets` | `tool_call_id TEXT UNIQUE` (nullable) | NULLs are distinct, so non-agent writers are unaffected |
| `ops_incidents` | `tool_call_id TEXT UNIQUE` | |
| `crm_followups` | `tool_call_id TEXT UNIQUE` | |
| `support_ticket_comments` | `id SERIAL PK`, `tenant`, `ticket_id FK`, `comment`, `tool_call_id TEXT UNIQUE`, `created_at` | append = its own row; parent `notes` column dropped |
| `crm_lead_notes` | `id SERIAL PK`, `tenant`, `lead_id FK`, `note`, `tool_call_id TEXT UNIQUE`, `created_at` | same |

Flattened `notes` returned to tools is `STRING_AGG(… ORDER BY created_at)` over a `LEFT JOIN`, so a parent with
zero notes still returns a row.

## 5. Graph `State` fields used by this feature

| Field | Type | Written by | Lifecycle | Note |
|-------|------|-----------|-----------|------|
| `require_approval` | `bool` | caller input | per call | opt-in gate; **not** set by the HTTP surface (the queued `turn` payload defaults it `False`; only the CLI `--hitl` mode sets it) |
| `approved` | `bool` | `human_approval` | every decision writes it; reset to `False` at the start of each new turn by `validate_input` (#62), never on a resume | read by `route_after_approval` |
| `cancelled` | `bool` | `human_approval` on cancel | **reset to `False` at the start of each new turn by `validate_input`, never on a resume (B3, fixed in #62)** | `route_after_approval` checks it **before** `approved` |
| `messages` | list | `human_approval` adds one `ToolMessage` per pending call on reject/cancel | per `add_messages` | |
| `ctx` | `SecurityCtx` | `validate_input` | per turn | **not** re-stamped on resume (resume starts inside `human_approval`); the *tool* reads ctx from the resume call's `config` |

## 6. The approval interrupt payload

`human_approval` calls `interrupt({"action": "approve_tool_calls", "tool_calls": [{"name": str, "args": dict}, …]})`.
`tool_call_id` is **not** included. The terminal stream event is
`{"type": "approval_required", "tool_calls": [{name, args}, …]}` (feature 001 turn-event-stream).
`GET …/pending_approval` returns `{tool_calls, resumable}` where `resumable = (state_schema_version == STATE_SCHEMA_VERSION)`.

## 7. Queue entities (Redis)

| Key | Type | TTL / bound | Purpose |
|-----|------|-------------|---------|
| `agent:requests:<domain>` | stream + consumer group | — | jobs for one domain's worker pool |
| `agent:requests:<domain>:dead` | stream | `maxlen ≈ 1000` (approximate trim) | dead-letter archive: `{original_entry_id, reason, payload}` |
| `agent:results:<request_id>` | stream | 300 s, refreshed on every write | one job's events; **not** deleted on terminal event |
| `agent:lock:<thread_id>` | string, `SET NX EX`, Lua compare-and-delete | `2 × REQUEST_TIMEOUT_SECONDS` (120 s) | at most one active job per conversation |
| `agent:cancel:<thread_id>` | string | 60 s | cooperative cancel of a streaming turn |
| `chat:submit_dedup:<thread_id>:<digest>` | string, `SET NX EX` | 10 s | identical-resubmission reuse |

### 7.1 Job payload (JSON in the stream field `payload`)

| `kind` | Fields | Producer |
|--------|--------|----------|
| `turn` (default) | `request_id`, `text`, `thread_id`, `ctx`, `require_approval`, `images` | `POST /chat/stream/queued` |
| `resume` | `request_id`, `thread_id`, `approved`, `ctx` | `POST /chat/resume` |
| `cancel` | `request_id`, `thread_id`, `ctx` | `POST /chat/cancel` |
| `turn_continue` | the original `turn` payload with `kind` flipped | the worker's own reclaim path only |

`domain` is never in the payload — it is inferred from the stream the job landed on. Reclaimed jobs gain an
internal `_reclaim_attempts` counter.

## 8. State machines

### 8.1 A pending action

```text
 proposed ──(should_continue)──► [bounced to model]    invalid name / >5 calls / skill-without-search
    │
    ├─ all read_only and require_approval off ──────► runs
    └─ any non-read_only (or undeclared) ───────────► PAUSED (interrupt)
                                                         ├─ approve ──► runs ──► agent
                                                         ├─ reject ───► one ToolMessage per call ──► agent
                                                         └─ cancel ───► one ToolMessage per call; cancelled=True ──► END   (cleared at the next turn's start — B3, fixed in #62)
```

### 8.2 Conversation vs. job

```text
 idle ──turn job──► running (lock held) ──done/error──► idle
                       │
                       ├─ interrupt ──► paused (lock released BEFORE the terminal event is published)
                       │                  ├─ resume job ─► running (lock held) ─► idle | paused again
                       │                  ├─ cancel job ─► idle  (cancelled=True is cleared at the next turn's start — B3, fixed in #62)
                       │                  └─ new message ─► refused: pending_approval (or proceeds if not resumable)
                       └─ worker dies ──► (job unacked) ──► reclaim after 240 s idle ──► classify (§8.3)
```

### 8.3 Reclaim classification (`_classify_reclaimed_turn`) — reads the checkpoint, runs nothing

| Observation | Action |
|-------------|--------|
| job kind `cancel` or `resume` | retry (always safe) |
| checkpoint unreadable | `dead_letter` |
| no `HumanMessage` anywhere | `retry_fresh` ⇒ republish as `turn` |
| last turn already ended in a final `AIMessage` with no tool calls | `dead_letter` |
| checkpoint paused at a real interrupt | `dead_letter` (belongs to a resume) |
| otherwise | `retry_continue` ⇒ republish as `turn_continue` (`astream_events(None, …)`) |
| `_reclaim_attempts ≥ MAX_AUTO_RECLAIM_RETRIES` (1) or not safe | publish `error{code:"worker_lost"}`, archive to DLQ, ack |

### 8.4 Unattended turn (B4 fixed in #63)

```text
 turn ─► pause #1 ─► auto-decline (resume False) ─► model re-requests ─► pause #2 ─► auto-decline ─► … (up to UNATTENDED_MAX_DECLINE_ROUNDS)
                                                                                    │
                       still asking at the ceiling ─► cancel_run ─► one explicit message "needs a person's approval, not done"
                       thread is never left paused; exactly one terminal event; every decline counts agent_unattended_pause_total
```
