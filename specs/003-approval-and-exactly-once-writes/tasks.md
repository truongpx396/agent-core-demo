---

description: "Task list for feature 003 — Mandatory Approval and Exactly-Once Writes (retrospective)"
---

# Tasks: Mandatory Approval and Exactly-Once Writes

**Input**: Design documents from `/specs/003-approval-and-exactly-once-writes/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principles II and IV are NON-NEGOTIABLE and Principle VII requires a regression test
for every bug fix. Two of this feature's defects (B3, B4) were *missed by the existing tests*, so the
open test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path is where it lives. Nothing `[x]`
  needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written
  first** (CLAUDE.md working rules): write it, watch it fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **A8** no alert for a rising
  unattended-pause rate (unblocked) · **A9** dedup sweep is manual · **A10** approvals unattributed · **A13** the `XAUTOCLAIM` reclaim path has no real-Redis test · **E2** dedup lookup not
  tenant-scoped · **R1** notification duplicate window. **Closed since this file was written:** B3 (#62), B4 (#63), A12 (#65), A11 (#66), A7 (#68).
- **Both defects (B3, B4, now fixed) failed closed: no unreviewed write occurred.** They were liveness/consistency defects, not
  safety holes — but B4 was a literal deviation from Principle II.
- Tasks needing Docker say `integration`. Paths are repo-relative.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US6 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] Dedup table — `tool_call_id` TEXT **PRIMARY KEY**, `tenant` TEXT NOT NULL, `thread_id` TEXT nullable, `tool_name` TEXT NOT NULL, `result` TEXT nullable, `created_at` TIMESTAMPTZ DEFAULT `now()`, index on `thread_id` — in `postgres-init/13-tool-call-dedup.sql`
- [x] T002 [P] Target-level keys — nullable `tool_call_id TEXT UNIQUE` on `support_tickets`, `ops_incidents`, `crm_followups` — in `postgres-init/14-tool-call-id-columns.sql`
- [x] T003 [P] Appends as rows — `support_ticket_comments` and `crm_lead_notes` each with `tenant`, parent FK, `tool_call_id TEXT UNIQUE`, `created_at`; parent `notes` columns dropped — in `postgres-init/15-append-notes-as-rows.sql`
- [x] T004 [P] Alert rules `ToolCallDedupDegraded` (`increase(agent_tool_dedup_degraded_total[15m]) > 0`, `for: 15m`) and `TeamChannelNotifyFailing` in `observability/prometheus/alerts.yml`
- [x] T005 [P] Queue/reclaim tunables in `app/core/config.py`: `chat_submit_dedup_ttl_seconds` (10), `chat_first_response_deadline_seconds` (30), `agent_worker_max_concurrency` (10), `agent_worker_reclaim_idle_seconds` (240), `worker_reclaim_interval_seconds` (60), `max_auto_reclaim_retries` (1) — **none of the six has an `.env.example` entry** (this task originally said they did; checked 2026-10-03 — feature 004 A3 tracks it; only `UNATTENDED_MAX_DECLINE_ROUNDS`, added in #63, has one)
- [x] T006 [P] The side-effect-tool checklist in `.claude/rules/side-effect-tools.md` (loaded when `tools.py`/`store.py`/`postgres-init/` change)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The capability model and the exactly-once wrapper every story depends on.

- [x] T007 `ToolCapability = Literal["read_only","mutating","outward"]`, the default-set `TOOL_CAPABILITIES`, `_arun_with_timeout` (15 s, scrubbed result), `_ctx_or_refuse` in `app/agent/tools.py`
- [x] T008 `_tool_capability` (an undeclared tool ⇒ `"outward"`, the only place the default is applied) and `_mandatory_gate_reason` in `app/agent/graph_loop_guards.py`
- [x] T009 `idempotent()` (atomic `INSERT … ON CONFLICT (tool_call_id) DO NOTHING RETURNING`, cached-result replay, fail-open + `agent_tool_dedup_degraded_total`), `MutatingToolTimedOut`, `sweep_stale_rows` in `app/agent/tool_idempotency.py`
- [x] T010 [P] Counters `agent_human_approval_total{decision}`, `agent_capability_gate_total{capability}`, `agent_unattended_pause_total`, `agent_tool_dedup_degraded_total`, `agent_worker_job_reclaimed_total{queue,outcome}`, `agent_team_channel_notify_total{sink,outcome}`, `agent_cancellation_total`, `agent_streaming_cancellation_total` in `app/core/metrics.py`
- [x] T011 [P] `ErrorCode` members used here (`pending_approval`, `thread_busy`, `worker_lost`, `cancelled`) in `app/core/errors.py` (envelope semantics: feature 001)

**Checkpoint**: Foundation ready — a tool can be declared, gated and wrapped.

---

## Phase 3: User Story 1 — Nothing with a side effect happens without a person's explicit yes (Priority: P1) 🎯 MVP

**Goal**: Every non-read-only (or undeclared) action pauses for a human decision; no setting bypasses it.

**Independent Test**: Request a read-only, a mutating and an unregistered action — only the first runs
(quickstart Tier 3 step 1; Tier 1 `test_routing.py`).

### Tests for User Story 1

- [x] T012 [P] [US1] Capability lookup (undeclared ⇒ outward), mandatory-gate reason (outward over mutating), mandatory gate regardless of `require_approval` — `tests/agent/test_routing.py` (`TestToolCapability`, `TestMandatoryGateReason`, `TestShouldContinueMandatoryGate`)
- [x] T013 [P] [US1] Invalid tool name and over-large batch bounce to the model before the gate in `tests/agent/test_routing.py` (`TestShouldContinueInvalidToolCall`, `TestShouldContinue`) and `tests/agent/test_safety_budgets.py` (`TestToolCallBudget`)
- [x] T014 [P] [US1] Approve runs the tool; reject returns to the agent without running it, in `tests/agent/test_graph_integration.py::TestHumanApprovalPath`
- [x] T015 [P] [US1] Every default-set tool has a declared capability; `add_note`/`remember` are registered in `tests/agent/test_tools.py::…test_every_tool_in_TOOLS_has_a_declared_capability`

### Implementation for User Story 1

- [x] T016 [US1] `should_continue` ordering — ceilings → fan-out → invalid names → skill-without-search → repeats → capability gate (`mandatory_reason or require_approval` ⇒ `human_approval`) — in `app/agent/graph_routing.py`
- [x] T017 [US1] `human_approval` (`interrupt({"action":"approve_tool_calls","tool_calls":[{name,args}]})`; three outcomes; `agent_human_approval_total`), `route_after_approval` (cancelled → `__end__`; approved → `tools`; else `agent`) in `app/agent/graph_hitl.py`
- [x] T018 [P] [US1] `_reject_tool_calls` — one `ToolMessage` per pending call — and the `too_many_tool_calls`/`invalid_tool_call` nodes in `app/agent/graph_tools.py`
- [x] T019 [P] [US1] Per-domain capability declarations in `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/tools.py`; sandbox tools capped at `outward` (`capability_overrides={}`) in `app/domains/sandbox_tools.py`; `ActionAllowlistPolicy` in `app/domains/policy.py`

**Checkpoint**: US1 delivers the mandatory gate on its own.

---

## Phase 4: User Story 2 — A write happens at most once for a logical action (Priority: P1)

**Goal**: A replayed write is a no-op, in two independent layers; a timeout steers to verify.

**Independent Test**: Same call id twice ⇒ one row/point; force a timeout ⇒ the agent is told to verify.

### Tests for User Story 2

- [x] T020 [P] [US2] First call runs, replay returns the cached result without running `fn`, claim race, fail-open on a store error with the counter, `MutatingToolTimedOut` on `TimeoutError`, sweep deletes old rows — `tests/agent/test_tool_idempotency.py` (`TestIdempotent`, `TestSweepStaleRows`)
- [x] T021 [P] [US2] The timeout message says the effect may already have been applied and to verify first, in `tests/agent/test_graph_utils.py`
- [x] T022 [P] [US2] Store statements carry `ON CONFLICT (tool_call_id)` and the tenant (fake cursors — **statement shape only**) in `tests/domains/support/test_store.py`, `tests/domains/ops/test_store.py`, `tests/domains/sales/test_store.py`
- [x] T023 [P] [US2] `add_note`/`remember` derive their point id from `tool_call_id` in `tests/agent/test_tools.py` (`TestAddNoteImpl`, `TestRememberImpl`)
- [x] T024 [P] [US2] The retention sweep script in `tests/scripts/test_tool_call_dedup_sweep.py`

### Implementation for User Story 2

- [x] T025 [US2] Every mutating/outward tool body — `_ctx_or_refuse` → `idempotent(tool_call_id, …)` → `_arun_with_timeout(_<tool>_impl, …)` — in `app/agent/tools.py` (`add_note`, `remember`), `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/tools.py` (all 15 statically declared tools and the four sandbox tools in each of the three domains; verified by script on 2026-10-02)
- [x] T026 [P] [US2] Row-level idempotence in the stores — `INSERT … ON CONFLICT (tool_call_id) DO NOTHING RETURNING id` with read-back (`create_ticket`, `log_incident`, `add_followup`), append rows (`add_comment`, `append_lead_note`, `mark_lead_lost`, `find_or_create_lead`), naturally idempotent UPDATEs — in `app/domains/support/store.py`, `app/domains/ops/store.py`, `app/domains/sales/store.py`
- [x] T027 [P] [US2] `_friendly_tool_error` dispatching on `MutatingToolTimedOut` ("Do NOT blindly call it again … check whether it already happened, using a read-only tool") in `app/agent/graph_utils.py`
- [x] T028 [P] [US2] Best-effort pivot notification — never raises, counted by sink/outcome — in `app/domains/notify.py`
- [x] T029 [P] [US2] Retention sweep `scripts/tool_call_dedup_sweep.py` (default 24 h) and Makefile target `tool-call-dedup-sweep` in `Makefile`
- [x] T030 [P] [US2] Retry only where "never landed" is provable — `CircuitBreaker.call(retry_on=…)` around the read-only sandbox listing and the crawler's connection errors — in `app/core/resilience.py`, `app/domains/sandbox_tools.py`, `app/ingestion/web_crawler.py`

### Open follow-ups for User Story 2 (not built)

- [ ] T031 [US2] **Principle VII known gap — write the real-Postgres test**: new `tests/integration/test_write_idempotency_real_postgres.py` (`integration` marker; Docker; `tests/containers.py::ensure_postgres`). Using the real `postgres-init` schema: (a) `support.store.create_ticket` twice with one `tool_call_id` ⇒ **one** row and the same returned id; (b) `add_comment` twice ⇒ one `support_ticket_comments` row; (c) `idempotent()` against the real `tool_call_dedup`: a first call runs, a second with the same id returns the cached `result` without running `fn`; two concurrent claimers ⇒ exactly one insert wins. It proves the constraint the hermetic fake-cursor tests only *describe*. Must self-skip, never fail, without Docker
- [ ] T032 [US2] **E2 — write the failing test first**: in `tests/agent/test_tool_idempotency.py` (and, once T031 exists, its real-Postgres twin) add `test_a_different_tenants_identical_call_id_never_receives_the_first_tenants_result` — claim `tool_call_id="call_1"` for tenant A with result `"A's secret"`, then call `idempotent(tool_call_id="call_1", ctx=<tenant B>, …)` and assert B does **not** get `"A's secret"`. Fails today (`_claim_or_cached_result` selects by id alone)
- [ ] T033 [US2] **E2 — decide and fix**: in `app/agent/tool_idempotency.py::_claim_or_cached_result` add `AND tenant = %s` to the read-back so a foreign tenant never receives another tenant's stored result. Because the primary key is `tool_call_id` alone, tenant B's `INSERT` still conflicts and B's read-back then finds nothing ⇒ B runs its own write (the same path as the accepted `result IS NULL` race, covered by the target-level layer). Record this collision semantics in `postgres-init/13-tool-call-dedup.sql`'s header comment, spec *Assumptions*, and research R24; delete plan row **E2**
- [ ] T034 [P] [US2] **R1 — decide**: should the three tools that send a team-channel message (`escalate_to_human` in `app/domains/support/tools.py`, `handoff_to_human` in `app/domains/sales/tools.py`, `post_to_team_channel` in `app/domains/ops/tools.py`) be send-once? Option (a): disclose and accept (docs only, folded into T065); option (b): a `notification_sent` row keyed by `tool_call_id` checked before `notify.post_to_team_channel` (new `postgres-init/16-notification-sent.sql`, failing test first in the matching `test_domain.py`). Record the decision in research R22 and delete plan row **R1** if (b)
- [ ] T035 [P] [US2] **A9 — make the sweep operable**: document the cron line for `make tool-call-dedup-sweep` in README *Deploying to production* and add a gauge for `tool_call_dedup` row count (so growth is visible) in `app/core/metrics.py`, set by `scripts/tool_call_dedup_sweep.py`; failing test first in `tests/scripts/test_tool_call_dedup_sweep.py`

**Checkpoint**: after T031–T033, Principle IV's layer 2 is *proven* and layer 1 no longer crosses tenants.

---

## Phase 5: User Story 3 — A paused conversation is durable and can be approved, rejected or cancelled (Priority: P1)

**Goal**: A pause survives restarts; approve/reject/cancel behave as specified; a new message never silently clears a pause.

**Independent Test**: Quickstart Tier 3 steps 2–8; Tier 2 `test_durable_checkpoint.py`.

### Tests for User Story 3

- [x] T036 [P] [US3] Durable pause across a "restart", async seeding, resume refused when not paused / on schema mismatch / while a thread is actively running — `tests/agent/test_durable_checkpoint.py` (`integration` tier)
- [x] T037 [P] [US3] Cancel of a paused run ends it outright; cancel of a streaming turn via the cooperative check; raw asyncio cancellation — `tests/agent/test_cancellation.py`, `tests/agent/test_graph_integration.py::TestHumanApprovalPath::…cancelled…`, `tests/agent/test_routing.py::TestRouteAfterApproval`
- [x] T038 [P] [US3] Concurrent HITL pause/resume round trip across 5 real worker processes in `tests/integration/test_worker_scaling.py` (`integration` tier)
- [x] T039 [P] [US3] The resume endpoint publishes a `resume` job with `approved`/`thread_id`, and the cancel endpoint sets the flag and publishes a `cancel` job, in `tests/api/test_api.py` (**`GET …/pending_approval` has no API-level test** — feature 002's FR-012 test task covers it)

### Implementation for User Story 3

- [x] T040 [US3] `resumability_error_async` / `_resumability_error_from_state` (paused = `state.next` **and** `task.interrupts`; schema-version mismatch only), `paused_approval_async`, `CANCEL_SENTINEL` in `app/agent/graph_hitl.py`
- [x] T041 [US3] `astream_events_turn` refuses a new message to a resumable pause with `ErrorCode.PENDING_APPROVAL` + `details.tool_calls` (proceeds with a `system_note` if not resumable); `astream_events_resume` (re-supplies ctx); `cancel_run`; `get_pending_approval` in `app/agent/runtime_stream.py`
- [x] T042 [US3] `POST /chat/resume` (rate-limited), `POST /chat/cancel` (never rate-limited; sets the flag **and** publishes a cancel job), `GET /chat/sessions/{thread_id}/pending_approval` in `app/api/main.py`; `ResumeRequest`/`CancelRequest`/`PendingApproval` in `app/api/schemas.py`; cancel flag helpers (`agent:cancel:<thread_id>`, 60 s) in `app/job_queue/queue.py`
- [x] T043 [P] [US3] Cooperative cancel — `_iterate_with_timeout(cancel_check=…)` raising `TurnCancelled` → `error{code:"cancelled"}` — in `app/agent/runtime_stream.py`; `_process_turn`/`_process_turn_continue` clear a stale flag and pass `cancel_check` in `app/job_queue/agent_worker.py`

### Open follow-ups for User Story 3 (not built)

- [x] T044 [US3] **B3 — regression test** (written first, confirmed failing; #62): `tests/agent/test_graph_integration.py::TestHumanApprovalPath::test_an_approval_on_a_later_turn_still_runs_after_an_earlier_cancel_on_the_same_thread` — cancel on turn 1, approve on turn 2 of the same thread, assert the approved call's `ToolMessage` exists and the run does not end at once; plus the per-turn-reset graph tests in `tests/agent/test_safety_budgets.py`
- [x] T045 [US3] **B3 — fix** (#62): `validate_input` in `app/agent/graph.py` resets `cancelled` and `approved` every new turn; a resume re-enters inside `human_approval` and skips it, so a pause's own decision is never cleared; `STATE_SCHEMA_VERSION` not bumped (no field added or removed)
- [x] T046 [US3] **A12 — failing tests first** (#65): `tests/agent/test_cancellation.py::TestAstreamEventsResumeCancellation` — a cancel after approval stops the turn before the approved tool runs; a cancel check that never fires still runs the approved tool once; no cancel check resumes exactly as before; `_process_resume` passes a working check bound to the thread id and clears a stale flag first (the last two in `tests/job_queue/test_agent_worker.py`)
- [x] T047 [US3] **A12 — fix** (#65): `cancel_check=None` on `astream_events_resume` in `app/agent/runtime_stream.py`, forwarded to `_run_graph_stream`; `_process_resume` in `app/job_queue/agent_worker.py` clears any stale flag left by the streaming phase and wires `is_cancelled`; `app/api/main.py` and `app/job_queue/queue.py` adjusted accordingly
- [ ] T048 [P] [US3] **A10 — decide**: an approvals audit record (`thread_id`, tenant, **principal who decided**, decision, the pending tool names plus a hash of their args, `decided_at`). Decide retention and whether args may be stored (PII), and whether it lives in `appdata` (new `postgres-init/16-approvals-audit.sql`). Record the decision in research R26
- [ ] T049 [US3] **A10 — failing test, then implement** (after T048): a test in `tests/agent/test_durable_checkpoint.py` (or a new hermetic `tests/agent/test_approval_audit.py`) that resolving an approval writes exactly one audit row carrying the resumer's principal; then write it from `astream_events_resume` / `cancel_run` (they already hold `ctx`; the node does not) in `app/agent/runtime_stream.py`, best-effort and fail-open like `usage_ledger.py`, with a counter and no message content in logs

- [ ] T050 [US3] **FR-010 / FR-009 / FR-012 — write the missing tests (found by `/speckit-analyze`; hermetic, none exist today)**: in a new `tests/agent/test_pending_approval.py`, using an in-memory graph returned by a patched `runtime.init_graph_async` (as `tests/agent/test_streaming_terminal_events.py` does) and a scripted model that requests the mutating `add_note` tool: (a) after a pause, `astream_events_turn("anything", same_thread, ctx)` yields exactly one `error` with `code == "pending_approval"` whose `details["tool_calls"]` names `add_note`, and the pending action is **still pending** afterwards (`paused_approval_async(...)` is not `None`) — never auto-cancelled; (b) with `STATE_SCHEMA_VERSION` monkeypatched to a different value so the pause is not resumable, the same call yields a `system_note` and then proceeds; (c) `astream_events_continue_turn(same_thread, ctx)` on a paused thread yields `pending_approval` and runs nothing; (d) `paused_approval_async` returns `None` for a completed or unknown thread and `{"tool_calls": [...], "resumable": True}` for a paused one; (e) `astream_events_resume` after a pause runs the tool under the **resumer's** ctx (assert the ctx the tool received is the one passed to the resume call, not the original's). These pin FR-010, FR-012 and FR-009 and complete T053's untested half

**Checkpoint**: US3 meets FR-004/FR-011 and SC-005/SC-010 (T044–T047 landed in #62 and #65); FR-010 is *verified* only after T050 (its API-level read, FR-012, was covered in #68 by `tests/api/test_pending_approval_endpoint.py`).

---

## Phase 6: User Story 4 — A crashed worker never duplicates a write (Priority: P2)

**Goal**: At-least-once delivery, no blind re-run: continue, don't restart.

**Independent Test**: Quickstart *Crash recovery*; Tier 1 `tests/job_queue/test_agent_worker.py`.

### Tests for User Story 4

- [x] T051 [P] [US4] Dispatch by `kind`; always ack; `TestClassifyReclaimedTurn` (fresh / continue / dead-letter cases); `TestHandleReclaimedJob` (retry cap, `worker_lost`); `TestReclaimLoop`; `TestSameThreadJobsAreSerialized`; concurrency limit — `tests/job_queue/test_agent_worker.py`
- [x] T052 [P] [US4] Queue helpers — thread lock, cancel flag, submission claim/release, reclaim, republish, dead letter — in `tests/job_queue/test_queue.py`; against real Redis in `tests/integration/test_queue_real_redis.py` (`integration` tier)
- [x] T053 [P] [US4] `astream_events_continue_turn` continues a checkpointed run **without re-executing an already-completed tool call** (real `AsyncPostgresSaver`, real `calculator`), in `tests/agent/test_durable_checkpoint.py::TestAstreamEventsContinueTurn` (its refusal of a thread paused at an interrupt is **not** tested — see T050)

### Implementation for User Story 4

- [x] T054 [US4] `process_request` — lock acquired per job (`THREAD_BUSY` on loss), ack in `finally`, error published on handler failure, lock released **before** the terminal event; `_classify_reclaimed_turn`, `_handle_reclaimed_job`, `_reclaim_loop` (`XAUTOCLAIM`, 240 s idle, cap 1, dead-letter + `worker_lost`) in `app/job_queue/agent_worker.py`
- [x] T055 [US4] Streams per domain, results streams (TTL 300 s, not deleted on terminal), `acquire_thread_lock`/`release_thread_lock` (Lua compare-and-delete, TTL `2 × REQUEST_TIMEOUT_SECONDS`), `claim_or_get_existing_submission`/`release_submission_claim`, `reclaim_stale_entries`, `republish_job`, `publish_dead_letter` (`maxlen ≈ 1000`), `read_results(first_event_deadline_seconds=…)` in `app/job_queue/queue.py`
- [x] T056 [US4] `astream_events_continue_turn` — `graph.astream_events(None, …)`, refuses a thread paused at a real interrupt with `PENDING_APPROVAL` — in `app/agent/runtime_stream.py`
- [x] T057 [P] [US4] Submission dedup and its compensating release at the HTTP edge in `app/api/main.py::chat_stream_queued`

### Open follow-ups for User Story 4 (not built)

- [ ] T058 [P] [US4] **A13 — write the real-Redis reclaim test (`integration` tier; Docker)**: new `tests/integration/test_queue_reclaim_real_redis.py` using `tests/containers.py::ensure_redis()` (Redis Stack) and the real `app/job_queue/queue.py`: publish more than one page of entries onto a domain stream, read some through a consumer group **without acking**, wait past a short `min_idle_ms`, then assert `reclaim_stale_entries` returns **all** abandoned entries exactly once (exercising the real `XAUTOCLAIM` pagination cursor that `tests/job_queue/test_queue.py`'s hand-written fake deliberately does not implement), that an entry acked by another consumer is not returned, and that `republish_job` + `publish_dead_letter` + ack leave the stream's pending list empty. Self-skip, never fail, without Docker. Then drop the "just enough of real XAUTOCLAIM" caveat from the fake's docstring or keep both

**Checkpoint**: US4 independently verifiable at the integration tier.

---

## Phase 7: User Story 5 — Unattended callers and background jobs never write unreviewed (Priority: P2)

**Goal**: Decline, never approve; cron bypasses the loop; sub-assistants read-only.

**Independent Test**: Tier 1 `test_agent_pause_handling.py`, `test_subagents.py`; quickstart Scenario B4 (expected to reproduce today).

### Tests for User Story 5

- [x] T059 [P] [US5] `astream_events_turn_unattended` forwards when never paused; swallows `approval_required`, resumes with `approved=False`, counts `agent_unattended_pause_total` (**mocked** streams) in `tests/agent/test_agent_pause_handling.py`
- [x] T060 [P] [US5] Sub-assistant catalog drops non-read-only/unknown tools, excludes `run_subagent`, in `tests/agent/test_subagents.py`
- [x] T061 [P] [US5] The chat-app channel collects the unattended stream in `tests/channels/test_telegram_channel.py`

### Implementation for User Story 5

- [x] T062 [US5] `astream_events_turn_unattended` (one-round auto-decline) in `app/agent/runtime_stream.py`; the chat-app channel's `_run_turn`, `_ctx_for_user` in `app/channels/telegram.py`
- [x] T063 [P] [US5] Read-only sub-assistant catalog resolution (declared non-`read_only` or unknown tools dropped with a warning, never upgraded; `run_subagent` excluded) in `app/agent/subagent_tools.py` and `app/agent/subagent_domain_tools.py`
- [x] T064 [P] [US5] Scheduled jobs as fixed pipelines that never enter the agent loop in `scripts/ops_digest.py`, `scripts/followup_sweep.py`; the one-shot `scripts/ops_investigate.py` documents that a gated call ends with an empty answer and no write

### Open follow-ups for User Story 5 (not built) — **B4**

> Candidate designs: research R10. Safety is unaffected either way (no write occurs).

- [x] T065 [US5] **B4 — failing tests first** (#63): `tests/agent/test_agent_pause_handling.py::TestUnattendedSecondPause` (`…a_second_pause_is_declined_too_and_the_turn_ends_normally`, `…a_model_that_keeps_requesting_the_write_is_cancelled_with_an_explicit_message`) driving the real `astream_events_turn_unattended`, and `tests/channels/test_telegram_channel.py` for the chat channel's reply
- [x] T066 [US5] **B4 — fix** (#63): the decline in `app/agent/runtime_stream.py::astream_events_turn_unattended` is a loop bounded by `UNATTENDED_MAX_DECLINE_ROUNDS` (new `Settings` field in `app/core/config.py`, default 3, in `.env.example`; each round counts `agent_unattended_pause_total` in `app/core/metrics.py`); at the ceiling the run is cancelled via `cancel_run` and one explicit message is emitted — exactly one terminal event; the Telegram channel's handling updated in `app/channels/telegram.py`
- [x] T067 [US5] **B4 — docs** (#63): the `astream_events_turn_unattended` docstring, the channel's header paragraph and `GRAPH_PATTERNS.md` corrected; spec *Known gaps* B4 moved to *Resolved since this spec was written*
- [ ] T068 [P] [US5] **A8 — alerts (after T066)**: add `UnattendedPauseRate` (sustained `rate(agent_unattended_pause_total[15m])`) to `observability/prometheus/alerts.yml`, same annotation style as `RetrievalDegraded`; validate once by hand with `promtool check rules` (the repo has no automated check) and say so in the PR

**Checkpoint**: US5 meets Principle II's "unattended callers MUST auto-decline a pause" (T065–T067 landed in #63); T068 (an alert on the pause rate) is now unblocked.

---

## Phase 8: User Story 6 — Adding a write tool is a checklist, and the checklist is enforced (Priority: P3)

**Goal**: The two NON-NEGOTIABLE principles stay true as tools are added.

**Independent Test**: A generic test fails if any non-`read_only` tool in any domain drops its ctx check or its `idempotent()` wrapper.

### Tests for User Story 6

- [x] T069 [P] [US6] Default-set capability coverage in `tests/agent/test_tools.py::…test_every_tool_in_TOOLS_has_a_declared_capability`; sandbox tools declared `outward` in `tests/domains/ops/test_domain.py::test_every_sandbox_tool_present_is_declared_outward` and `tests/domains/sales/test_domain.py`; domain manifests in `tests/domains/{support,ops,sales}/test_domain.py`, `tests/domains/test_registry.py`

### Implementation for User Story 6

- [x] T070 [US6] The checklist itself — tier, ctx check, `idempotent()`, row-level uniqueness, tenant-scoped SQL, tests, docs — in `.claude/rules/side-effect-tools.md`; the Spec Kit gate (a feature adding a write tool states its tier, tenant scoping and duplicate story) in `.specify/memory/constitution.md`

### Open follow-ups for User Story 6 (not built)

- [x] T071 [US6] **A7 — the contract test** (#68): `tests/domains/test_write_tools_contract.py` — enumerates every non-`read_only` tool of every plugin in `app/domains/registry.py::DOMAINS` (56 parametrized cases) and asserts each refuses without a valid identity and does nothing, and routes its real work through `idempotent()` with the injected `tool_call_id` and its own name (`test_the_inventory_found_the_write_tools`, `test_every_write_tool_has_sample_arguments`, `test_a_write_tool_refuses_without_a_valid_context_and_does_nothing`, `test_a_write_tool_runs_its_work_through_idempotent_with_its_call_id_and_name`); mutation-checked
- [x] T072 [P] [US6] **Capability coverage for domain sets** (#68): `tests/domains/test_write_tools_contract.py::_write_tools` counts a tool a plugin leaves out of its capability mapping as `outward` (the default `should_continue` applies), so an unmapped tool is in scope; a new write tool without sample arguments fails with a pointer to `.claude/rules/side-effect-tools.md`

**Checkpoint**: Principle II/IV's tool-layer obligations are enforced, not just audited (T071–T072 landed in #68).

---

## Phase 9: Polish & Cross-Cutting Concerns

- [x] T073 [P] Pattern entries with their motivating bugs — patterns 8, 10, 15, 16, 36, 43, 46, 47 and the "Extending Further" duplicate-side-effect rounds — in `GRAPH_PATTERNS.md`; queue/worker design in `WORKER_CONCURRENCY.md`
- [x] T074 [P] **A11 — corrected the docs** (#66): `GRAPH_PATTERNS.md` "Extending Further" and the two comments (`app/core/metrics.py`, `app/core/config.py`) now describe `_classify_reclaimed_turn` / `astream_events_continue_turn` instead of the removed `_is_safe_to_retry_turn`; other drift fixed in the same change (the recursion limit, the node count, the ingest-reclaim policy, moved file paths)
- [x] T075 [P] **Open gaps disclosed in the project docs** (#66): thirteen verified gaps listed in `GRAPH_PATTERNS.md` "Extending Further" and a short list in the README, each stating how it was established; entries for fixed gaps are deleted by the fixing PR
- [x] T076 [P] Ran `/speckit-analyze` (read-only) over `spec.md`, `plan.md`, `tasks.md` on 2026-10-02 and reconciled what it found — see this feature's `checklists/requirements.md` *Validation iterations* for the findings, the corrections made, and the items deliberately left for a decision (requirement-id traceability tags; `promtool check rules` in CI)
- [ ] T077 Run `specs/003-approval-and-exactly-once-writes/quickstart.md` Tier 2 and Tier 3 (incl. *Replay safety* and *Crash recovery*) on a machine with Docker and a native Ollama, and record the result in the PR that closes the open follow-ups (Tier 1: 400 passed on 2026-10-02, 465 on 2026-10-03 after #62–#69; Scenarios B3 and B4 reproduced the defects)

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup → Foundational → stories.** Foundational blocks every story.
- **US1, US2, US3** (P1) need only Phase 2 and are mutually independent; US3's tests reuse US1's graph harness.
- **US4** (P2) needs US2's wrapper (a replayed `resume` is safe *because* of it) and US3's checkpointing.
- **US5** (P2) needs US1 (the gate) and US3 (`cancel_run`); T066's auto-cancel reuses it.
- **US6** (P3) is independent of the others at the code level.
- **Polish** last — except **T074** and **T075**, which landed first (#66).

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T074, T075 | **done — #66** | — |
| 2 | T044–T045 (B3) | **done — #62**; T050 (FR-010 tests) remains | — |
| 3 | T065–T067 (B4) | **done — #63** | — |
| 4 | T046–T047 (A12) | **done — #65** | — |
| 5 | T071–T072 (A7) | **done — #68** | — |
| 6 | T031 | new integration test | Docker; independent |
| 6b | T058 (A13) | new `tests/integration/test_queue_reclaim_real_redis.py` | Docker; independent; the primitive crash recovery rests on |
| 7 | T032–T033 (E2) | `tool_idempotency.py`, SQL header comment, tests | after T031 recommended |
| 8 | T048–T049 (A10) | `runtime_stream.py`, new migration, tests | needs a retention decision |
| 9 | T068 (A8) | `alerts.yml` | unblocked — PR 3 landed |
| — | T034 (R1), T035 (A9) | decision / small | bundle opportunistically |

PRs 1, 2, 4, 5, 6 are mutually independent; run them in parallel.

### Parallel opportunities

- Setup T001–T006 and Foundational T010–T011 are [P].
- After Phase 2, US1/US2/US3 in parallel; within a story every test task is [P].

## Parallel Example: User Story 2

```bash
# Tests together (different files):
Task: "T020 Wrapper in tests/agent/test_tool_idempotency.py"
Task: "T022 Stores in tests/domains/support/test_store.py"
Task: "T024 Sweep in tests/scripts/test_tool_call_dedup_sweep.py"
# Implementation together:
Task: "T026 Row-level idempotence in app/domains/support/store.py"
Task: "T027 Timeout message in app/agent/graph_utils.py"
Task: "T029 Sweep in scripts/tool_call_dedup_sweep.py"
```

## Implementation Strategy

### As-built order (what happened)

Opt-in approval first (`--hitl`) → mandatory gate when `add_note` arrived (pattern 15) → cancel as a third outcome
(36) → queue and workers (43) → then the duplicate-write rounds, each found by an audit after the previous one:
reclaim, `idempotent()`, target-level keys, appends as rows, soft-timeout steering, continue-don't-restart,
submission dedup (see research R11). The two defects (B3, B4) sit in the *recovery transitions* — cancel followed
by a later approval, and a second unattended pause — which only a two-step scenario exercises.

### Closing the open follow-ups (what to do next)

1. ~~PR 1 (docs)~~ — done in #66.
2. ~~PR 2 (B3)~~ — done in #62.
3. ~~PR 3 (B4)~~ — done in #63.
4. ~~PR 5 (A7)~~ — done in #68; **PR 6** (the real-constraint integration test, T031) and PR 6b (the real-Redis reclaim test, T058) remain, so the NON-NEGOTIABLE principles stop depending on review alone.
5. ~~PR 4 (A12)~~ — done in #65; **PRs 7, 8, 9** remain — tenant scoping of layer 1, auditability, alerting.
6. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 (T001–T030) is the minimum that satisfies Principles II and IV at the tool layer; **US3** makes the gate
durable and cancellable. Neither B3 nor B4 weakened the safety property, and both are now fixed (#62, #63): an approver's decision after a prior cancel is honored, and an unattended conversation is never left paused.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 was re-run — 400 passed on 2026-10-02 and 465 passed on 2026-10-03 after #62–#69.
- Tier 2/3 and the real-constraint claims are **not** verified by this batch.
- Features 001 (pipeline) and 002 (identity, ownership) own behavior this feature relies on; **B2** in feature 002
  interacted with the approval authority (who may resume) and was fixed in #67.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks.
