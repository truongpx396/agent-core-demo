# Implementation Plan: Mandatory Approval and Exactly-Once Writes

**Branch**: `003-approval-and-exactly-once-writes` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/003-approval-and-exactly-once-writes/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

Two NON-NEGOTIABLE principles, implemented as two cooperating mechanisms.

**Approval.** Every tool carries a tier in a `TOOL_CAPABILITIES` mapping. After each model round,
`should_continue` checks the pending batch: any call that is not `read_only` — and any call to an
undeclared tool, which defaults to `outward` — routes to the `human_approval` node, which calls
LangGraph's `interrupt()`. The checkpointer (Postgres) persists the pause; a caller resumes with
`Command(resume=True | False | "cancelled")`. `route_after_approval` sends `cancelled` straight to
`__end__`. Unattended callers use `astream_events_turn_unattended`, which resumes once with `False`.
Delegated sub-assistants are built from `read_only` tools only.

**Exactly-once.** Every non-read tool body is `ctx check → idempotent(tool_call_id, fn) → timed impl`.
`idempotent()` claims the provider-assigned call id with an atomic `INSERT … ON CONFLICT DO NOTHING`
into `tool_call_dedup` and returns the first call's stored result on a replay. Beneath it, the write is
idempotent at the target: `tool_call_id TEXT UNIQUE` + `ON CONFLICT DO NOTHING` for inserts and
appends, `uuid5(tool_call_id)` point ids for vector writes, naturally idempotent updates. A soft
timeout becomes `MutatingToolTimedOut`, which steers the agent to *verify* rather than retry. The
queue worker acks only after a job finishes; a crashed `"turn"` is classified from its checkpoint and
**continued** (`graph.astream_events(None, …)`), never restarted.

The plan records honestly that two liveness defects (**B3**, **B4**) and several coverage gaps sit
around an approval gate whose *safety* property — no unreviewed write — holds in every case examined.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `langgraph==0.2.76` (`interrupt`, `Command(resume=…)`, `RetryPolicy`),
`langgraph-checkpoint-postgres==2.0.25` (`AsyncPostgresSaver`; per-task pending writes are what make
"continue, don't restart" safe), `langchain-core==0.3.86` (`InjectedToolCallId`), `psycopg[binary,pool]`
(`ON CONFLICT`), `redis==8.1.0` (Streams consumer groups, `XAUTOCLAIM`, a Lua compare-and-delete lock
release), `qdrant-client==1.19.0` (upsert-by-id), `httpx` (webhook), `mcp==1.29.0` (sandbox bridge),
`fastapi==0.141.1`.

**Storage**: Postgres `appdata` — `tool_call_dedup` (PK `tool_call_id`) and the `tool_call_id TEXT UNIQUE`
columns on `support_tickets`, `ops_incidents`, `crm_followups`, `support_ticket_comments`,
`crm_lead_notes`; Postgres `checkpointer` — graph state and pending writes; Redis — `agent:requests:<domain>`
streams, per-request results streams, `agent:lock:<thread_id>`, `agent:cancel:<thread_id>`, submission-dedup
keys, dead-letter streams; Qdrant — `uuid5(tool_call_id)` points.

**Testing**: pytest hermetic tier for routing, the wrapper, stores (fake cursors — SQL shape only), worker
dispatch and reclaim classification (`tests/agent/test_routing.py`, `test_tool_idempotency.py`,
`test_cancellation.py`, `test_agent_pause_handling.py`, `test_durable_checkpoint.py`,
`tests/job_queue/test_agent_worker.py`, `test_queue.py`, `tests/domains/*/test_store.py`); `integration`
tier with real Postgres/Redis for `tests/integration/test_queue_real_redis.py` and
`test_worker_scaling.py` (concurrent HITL pause/resume across 5 worker processes). Against real services:
consumer-group delivery (`tests/integration/test_queue_real_redis.py`), the per-thread lock and concurrent same-thread
turns (`tests/agent/test_concurrent_turns.py`, real Redis + Postgres), and the scaled-stack pause/resume (whose worker
*subprocesses* receive a real `APPDATA_DATABASE_URL` and are not subject to the in-process mocks, so `idempotent()`
runs against real Postgres there, unasserted). **Not** tested against a real service: the target-level
`tool_call_id UNIQUE … ON CONFLICT` constraints, and the `XAUTOCLAIM` reclaim path — `reclaim_stale_entries` is tested
only against a hand-written fake that its own docstring says differs from real Redis's paginated form (A13).

**Target Platform**: Linux containers; N independently scaled `agent-worker` replicas per domain.

**Project Type**: Web service + queue workers + chat-bot channel + cron scripts.

**Performance Goals**: None asserted. Measured: 250 concurrent queued turns and a concurrent HITL
pause/resume round trip across 5 real worker processes complete correctly
(`tests/integration/test_worker_scaling.py`).

**Constraints**: tool soft timeout 15 s; turn timeout 60 s; `THREAD_LOCK_TTL_SECONDS = 2 × turn timeout`
(120 s); `CANCEL_FLAG_TTL_SECONDS = 60`; `AGENT_WORKER_RECLAIM_IDLE_SECONDS = 240` (deliberately above the
lock TTL); `WORKER_RECLAIM_INTERVAL_SECONDS = 60`; `MAX_AUTO_RECLAIM_RETRIES = 1`;
`CHAT_SUBMIT_DEDUP_TTL_SECONDS = 10`; first-event deadline 30 s; dedup sweep default retention 24 h;
dead-letter stream `maxlen ≈ 1000`.

**Scale/Scope**: 15 statically declared mutating/outward tools across the default set and three example
domains, plus 4 sandbox tools exposed by each of those domains.

**Unknowns**: none — every value is read from the repository.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | Yes — every write carries tenant | **PASS with E2 and a B2 interaction** | Every write path checks ctx first (`_ctx_or_refuse`) and takes `tenant`/`principal` from ctx, never from an argument. **E2**: `tool_idempotency._claim_or_cached_result` selects `WHERE tool_call_id = %s` with no tenant predicate — safe only if provider call ids are globally unique (spec *Assumptions*). **B2 interaction** (feature 002): the approver is not checked as the conversation's owner, and an approved write runs under the *resumer's* ctx. |
| II | Mandatory approval (NN) | **Primary** | **PASS on the gate; 2 liveness defects (B3, B4), one a literal deviation** | **Gate**: `TOOL_CAPABILITIES` (`app/agent/tools.py`) + per-domain mappings; `should_continue` → `_mandatory_gate_reason` routes any non-`read_only` call to `human_approval` regardless of `require_approval`; an undeclared tool is `outward` (`graph_loop_guards._tool_capability`); no flag disables it. **Subagents** restricted at catalog build (`app/agent/subagent_tools.py`: non-`read_only` or unknown tools dropped with a warning; `run_subagent` excluded). **Cron** calls fixed pipelines (`scripts/followup_sweep.py`, `scripts/ops_digest.py`). **B3** (reproduced): `State.cancelled` is set on a cancel and never reset, and `route_after_approval` checks it first, so a *later approved* call ends the run without running — it fails *closed* (no unreviewed write) but contradicts the approver's decision and strands a dangling tool request. **B4** (reproduced): `astream_events_turn_unattended` declines **one** pause; a model re-request produces a second pause that the chat-app channel ignores, stranding the conversation and sending no reply. Constitution II says unattended callers "MUST auto-decline a pause", so a second, un-declined pause is a literal deviation. Both fail closed: **no unreviewed write occurs in either case examined.** |
| III | Fixed, typed tools | Yes | **PASS** | Every write tool declares a Pydantic `args_schema` with `tool_call_id: Annotated[str, InjectedToolCallId]` in both the schema and the signature; target identity derived by code (`_tool_call_point_id`, `RETURNING id`); sandbox surface wrapped behind four flat tools, capability capped at `outward` (`app/domains/sandbox_tools.py`, `capability_overrides={}`). |
| IV | Exactly-once side effects (NN) | **Primary** | **PASS at the tool layer; E2, residual window R1, gap A7** | `idempotent()` wraps **all 15** statically declared mutating/outward tools **and** the four sandbox tools in each of the three domains (audited by script: every `TOOL_CAPABILITIES` non-`read_only` entry appears as an `idempotent(tool_name=…)`). Second layer: `tool_call_id UNIQUE … ON CONFLICT DO NOTHING` (`postgres-init/14-…`, `15-…`), `uuid5` points, appends as rows, `STRING_AGG` on read. `MutatingToolTimedOut` + `_friendly_tool_error` ("verify, don't retry"). Retries restricted to calls that never landed: `CircuitBreaker.call(retry_on=…)` wraps only the **read-only** MCP catalog listing (`sandbox_tools.load_sandbox_tools`) and the crawler's connection errors (`web_crawler.py`) — never a write. Crash recovery **continues** (`runtime_stream.astream_events_continue_turn`; `agent_worker._classify_reclaimed_turn`). Dedup-store failure fails open with `agent_tool_dedup_degraded_total` and the `ToolCallDedupDegraded` alert. **R1**: `escalate_to_human`, `handoff_to_human` and the ops `post_to_team_channel` tool send a team-channel message (`notify.post_to_team_channel`) that has *no* target-level uniqueness — protected by layer one only. **A7**: no test fails if a write tool drops `idempotent()` or its ctx check. |
| V | Bounded, observable failure | Yes | **PASS with 2 advisories** | Bounded: lock TTL, cancel-flag TTL, first-event deadline, `MAX_AUTO_RECLAIM_RETRIES`, bounded dead-letter stream, per-tool timeout. Observable: `agent_human_approval_total{decision}`, `agent_capability_gate_total{capability}`, `agent_unattended_pause_total`, `agent_tool_dedup_degraded_total` (+ alert), `agent_worker_job_reclaimed_total{queue,outcome}`, `agent_team_channel_notify_total{sink,outcome}` (+ alert `TeamChannelNotifyFailing`). **A8**: nothing alerts on a *stranded* unattended conversation (B4) or on a rising `agent_unattended_pause_total`; **A9**: dedup rows are removed only by a manually run sweep (no scheduler), so the table grows unbounded unless an operator runs it. |
| VI | Untrusted content is data | Yes — why the gate exists | **PASS** | A poisoned document can propose a write but cannot execute one; approval args are shown to the human (`human_approval` interrupt payload carries `name` and `args`). |
| VII | Test discipline | Yes | **PASS with the known gap and A7** | Hermetic coverage of routing, the wrapper, reclaim classification, worker dispatch. Real Postgres/Redis in `test_worker_scaling.py` for a concurrent pause/resume. **Known gap (constitution)**: store tests use fake cursors, so the reliance on the target-level `UNIQUE`/`ON CONFLICT` constraints is stated in comments but has no `integration` test. Consumer-group delivery and the thread lock *do* have real-Redis tests; **the `XAUTOCLAIM` reclaim path does not** (**A13**: only a fake that differs from real Redis's paginated form). **A7** as above. **B3/B4 were not caught** because the cancel tests exercise the cancel itself and the unattended tests mock `astream_events_turn`/`astream_events_resume` rather than driving a real graph through a second pause. |
| VIII | Why-first docs, honest gaps | Yes | **PASS with new disclosures** | Patterns 8, 15, 36, 43 and the "Extending Further" duplicate-side-effect rounds carry their motivating bugs. B3, B4, A7, E2's lookup shape, the notification duplicate window R1 and "approvals are unattributed" are **not** disclosed in `GRAPH_PATTERNS.md`/README today; they are disclosed here. **A11**: the docs also *conflict* with the code on crash recovery (a removed function is still described), which Governance says MUST be corrected. |

**Gate result (pre-research)**: no *safety* violation of II or IV found. Two literal/liveness defects (B3, B4)
and gaps (E2, R1, A7–A9) are recorded in Complexity Tracking. Under Governance they are avoidable, so
they are **open defects, not justified exceptions**. The plan proceeds because it describes shipped
code; the constitution's gate would require B4 to be fixed (or justified) before a PR that introduced it
could merge.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged on II/IV. The
per-tool matrix in `data-model.md` §4 is what exposed R1 (three tools whose *second* layer does not cover
the notification they send). `contracts/approval-protocol.md` makes B3 visible as a state-machine defect
(the `cancelled` marker has no reset transition).

## Project Structure

### Documentation (this feature)

```text
specs/003-approval-and-exactly-once-writes/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the real incidents behind each
├── data-model.md                  # Phase 1 — capabilities, per-tool second layer, dedup table, state machines
├── quickstart.md                  # Phase 1 — runnable approval / replay / crash / B3 / B4 checks
├── contracts/
│   ├── approval-protocol.md       # interrupt payload, three decisions, resume/cancel/pending endpoints, errors
│   ├── write-tool-contract.md     # the exactly-once checklist every mutating/outward tool must satisfy
│   └── queue-job-protocol.md      # job kinds, delivery, reclaim classification, lock, dead letter
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── agent/
│   ├── tools.py                  # TOOL_CAPABILITIES (default set), _ctx_or_refuse, add_note/remember
│   ├── graph_routing.py          # should_continue: ceilings → fan-out → invalid → repeat → mandatory gate
│   ├── graph_loop_guards.py      # _tool_capability (undeclared ⇒ outward), _mandatory_gate_reason
│   ├── graph_hitl.py             # human_approval, route_after_approval, CANCEL_SENTINEL,
│   │                             #   resumability_error_async, paused_approval_async
│   ├── graph_tools.py            # _reject_tool_calls (one ToolMessage per pending call)
│   ├── tool_idempotency.py       # idempotent(), MutatingToolTimedOut, sweep_stale_rows
│   ├── graph_utils.py            # _friendly_tool_error (verify-don't-retry message)
│   ├── runtime_stream.py         # astream_events_turn / _unattended / _resume / _continue_turn / cancel_run
│   ├── subagent_tools.py         # read_only-only catalog resolution; run_subagent excluded
│   └── subagent_domain_tools.py
├── domains/
│   ├── {support,ops,sales}/tools.py   # per-domain TOOL_CAPABILITIES + write tools
│   ├── {support,ops,sales}/store.py   # ON CONFLICT (tool_call_id) writes
│   ├── notify.py                 # best-effort outward pivot send (never raises, counted)
│   ├── sandbox_tools.py, sandbox_session.py   # outward cap; breaker around the read-only listing only
│   └── policy.py
├── job_queue/
│   ├── queue.py                  # streams, results, thread lock, cancel flag, submission dedup, reclaim, DLQ
│   └── agent_worker.py           # process_request, _classify_reclaimed_turn, _handle_reclaimed_job, _reclaim_loop
├── channels/telegram.py          # unattended channel (B4)
├── api/main.py                   # POST /chat/resume, /chat/cancel, GET …/pending_approval
└── core/{errors,metrics,resilience}.py

postgres-init/
├── 13-tool-call-dedup.sql        # tool_call_dedup (PK tool_call_id)
├── 14-tool-call-id-columns.sql   # tool_call_id UNIQUE on tickets / incidents / follow-ups
└── 15-append-notes-as-rows.sql   # appends become keyed rows

scripts/tool_call_dedup_sweep.py  # retention sweep (manual); followup_sweep.py, ops_digest.py (fixed pipelines)
observability/prometheus/alerts.yml   # ToolCallDedupDegraded, TeamChannelNotifyFailing
.claude/rules/side-effect-tools.md    # the checklist

tests/
├── agent/{test_routing,test_tool_idempotency,test_cancellation,test_agent_pause_handling,
│          test_durable_checkpoint,test_graph_utils,test_tools}.py
├── job_queue/{test_agent_worker,test_queue}.py
├── domains/*/test_store.py       # fake cursors
├── integration/{test_queue_real_redis,test_worker_scaling}.py
└── (missing) tests/domains/test_write_tools_contract.py   → task (A7)
```

**Structure Decision**: No new package. The approval gate is a graph node plus a routing predicate; the
exactly-once layer is one wrapper plus a per-store SQL discipline; delivery semantics live in the worker.
The structural fact that makes both defects possible is that **liveness state is carried in `State`
fields and in channel code** (`cancelled`, the unattended helper's one-shot decline) while the *safety*
state (the pause itself) lives in the checkpointer — safety was designed carefully; recovery transitions
were not exercised end to end.

## Complexity Tracking

> Filled because the Constitution Check found two liveness defects (one literal deviation) and several
> gaps. Defects are listed without a justification column: they are simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B3 (defect, open)** — `State.cancelled` is never reset; after one cancelled approval a later *approved* action ends the run without running (reproduced). Fails closed. | Not needed — `validate_input` resets many per-turn fields but `cancelled` (and `approved`) were never added to that list. | Fix is one line plus a test that drives a **second approval on the same thread** (the existing tests assert the cancel itself and `route_after_approval` in isolation). Reset `cancelled` and `approved` in `validate_input` — but **not** on a resume — and add a graph-level regression test first. Its own PR. |
| **B4 (defect, open)** — an unattended conversation can be stranded by a second pause; the chat-app user gets no reply and cannot recover (reproduced at function level). Literal deviation from Principle II's "unattended callers MUST auto-decline a pause". | Not needed — `astream_events_turn_unattended` was written as "one-round auto-decline"; its docstring says a second pause is "left to the model's own next response", which the code does not do. | Options in `research.md` R10: loop the auto-decline until the turn ends (bounded by a round cap), and/or reply with an explicit "not approved" message when the stream ends on `approval_required`, and/or cancel a stranded thread. Needs a decision and a test that scripts a model re-requesting the write. |
| **E2** — dedup lookup not tenant-scoped. | Provider-assigned ids assumed unique; table's `tenant` column is metadata. | Adding `AND tenant = %s` is one predicate but changes the meaning of a cross-tenant collision (a miss ⇒ a second real write). Decide, then test with two tenants sharing an id. |
| **R1** — three tools send a team-channel message with no target-level uniqueness; layer one only. | The message is a best-effort *pivot* after an already-committed write (`notify.py` docstring); a unique key on a log/webhook has nowhere to live. | A send-once key would need its own store (e.g. a `notification_sent` row keyed by `tool_call_id`, checked before sending). Worth it only if duplicate team messages matter; at minimum disclose it. |
| **A7** — no test fails if a write tool drops `idempotent()` or its ctx check. | Tests were written per behavior, not as a contract over *all* tools. | A single generic test over every domain plugin's non-`read_only` tools is cheap (see `tasks.md`). |
| **A8** — no alert for a stranded unattended conversation or a rising unattended-pause rate. | Moot while B4 stands. | Add after B4 is fixed; an alert on a bug is worse than fixing the bug. |
| **A9** — `tool_call_dedup` retention is a manually run script. | Rows need only outlive `AGENT_WORKER_RECLAIM_IDLE_SECONDS`; the 24 h default is generous. | A scheduler is deployment-specific; at minimum document the cron line and add a table-size gauge. |
| **A13** — the crash-recovery primitive (`reclaim_stale_entries` → `XAUTOCLAIM`, including its pagination cursor) is tested only against a hand-written fake whose docstring says it is *"just enough of real XAUTOCLAIM"* and not paginated like the real one; no integration test references it. | Reclaim was developed hermetically; the real-Redis tests that exist target delivery and locking. | A real-Redis test (`tests/containers.py::ensure_redis()`) that abandons an entry and reclaims it, with more entries than one page, is small; it is the only way to verify the third-party behavior Principle VII says MUST be verified (task in `tasks.md`). |
| **A12** — a turn streaming *after an approval* cannot be cancelled: the resume path has no cancel check. | The cancel flag was built for new/continued turns; resume was treated as short. | Pass a `cancel_check` through `astream_events_resume` and have `_process_resume` clear/poll the flag; failing test first (cancel while a post-approval turn streams). |
| **A11** — `GRAPH_PATTERNS.md` and two code comments still describe `_is_safe_to_retry_turn`, which no longer exists; the doc says a turn that ran a write is refused, the code *continues* it. | Documentation was not updated when "continue, don't restart" replaced the safety scan. | Pure docs; violates Principle VIII's "conflicting documents MUST be corrected". Do it first (task). |
| **A10** — approvals are unattributed (decision counted by outcome only; no approver, time or action recorded). | Metrics were the only audit surface built; B2 (feature 002) means the approver is not even verified as the owner. | An approvals audit row (`thread_id, tool_calls hash, decision, ctx.principal, at`) is small and would make the gate auditable. Decision needed on retention/PII. |
