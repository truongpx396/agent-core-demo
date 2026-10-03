# Research: Mandatory Approval and Exactly-Once Writes

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-02

**Status**: Retrospective — decisions reconstructed from the code, its comments and the
`GRAPH_PATTERNS.md` patterns 8, 15, 36, 43 and the "Extending Further" duplicate-side-effect rounds. Each
entry names its evidence. **No `NEEDS CLARIFICATION` remains.** R23–R27 (Part C) are *findings* from verifying
the as-built system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**.

---

## Part A — The approval gate

### R1. The gate is mandatory, not opt-in

- **Decision**: `should_continue` routes any pending call whose tier is not `read_only` to
  `human_approval` **unconditionally**; the opt-in `require_approval` flag can only add pauses for read-only
  calls. No flag, env var or per-domain setting disables the mandatory route.
- **Rationale**: A RAG agent is exposed to untrusted text on essentially every turn; write capability on
  top is "one gamble away from an untrusted document steering a real write". The gate began as an opt-in
  (`--hitl`) and became mandatory when the first mutating tool (`add_note`) arrived.
- **Alternatives considered**: opt-in only (rejected: a missed opt-in is a missed control); per-domain
  setting (rejected: constitution II forbids any bypass).
- **Evidence**: `app/agent/graph_routing.py::should_continue`; `graph_loop_guards._mandatory_gate_reason`;
  pattern 15; `tests/agent/test_routing.py::TestShouldContinueMandatoryGate`.

### R2. An undeclared tool is `outward`

- **Decision**: `_tool_capability` returns `"outward"` for any name missing from the mapping — "the only
  place this default is applied".
- **Rationale**: Forgetting a declaration should fail toward extra caution. `_invalid_tool_call_names` is
  stricter still: a name that is not a registered tool *at all* is bounced to the model before it can reach
  a human who could not meaningfully approve it (seen with small models emitting hallucinated names).
- **Evidence**: `app/agent/graph_loop_guards.py::_tool_capability`; `graph_tools.py::invalid_tool_call`.

### R3. A rejection must answer every pending call

- **Decision**: `human_approval` (rejected/cancelled) and the over-budget/invalid-name nodes all return
  exactly one `ToolMessage` per pending `tool_call` via `_reject_tool_calls`.
- **Rationale**: A pending `tool_call` with no matching `ToolMessage` makes the next provider call fail
  validation. This is the recurring "gotcha" behind patterns 8 and 10.
- **Evidence**: `app/agent/graph_tools.py::_reject_tool_calls`; `.claude/rules/runtime-reliability.md`.

### R4. Cancel is a third outcome, not a rejection

- **Decision**: `Command(resume=True | False | "cancelled")`. `route_after_approval` checks `cancelled`
  **first** and routes to `__end__`; a rejection routes back to `agent`.
- **Rationale**: A rejection still gives the model a turn to react ("rejected — try something else"). A
  human changing their mind needs a *guarantee* the run stops: "a caller-initiated abort, deliberately
  distinct from rejection".
- **Consequence recorded**: the marker is a persistent `State` field that nothing resets — **B3** (R22; fixed in #62).
- **Evidence**: `app/agent/graph_hitl.py`; pattern 36; `tests/agent/test_routing.py::TestRouteAfterApproval`.

### R5. A durable pause, and "paused" is not `state.next`

- **Decision**: The pause lives in the Postgres checkpointer. A thread counts as paused only if
  `state.next` **and** some `state.tasks[i].interrupts`.
- **Rationale**: Verified against a real race: `state.next` is truthy for *any* mid-run checkpoint. Trusting
  it let a concurrent `Command(resume=…)`/cancel start a second competing Pregel execution against the same
  checkpoint; the second call drove the turn to completion silently while the first caller's
  `astream_events()` got no further tokens, and an unrelated resume value was consumed as ordinary input with
  no exception. `paused_approval_async` deliberately does **not** reuse `resumability_error_async` because
  the latter increments `agent_checkpoint_issue_total{reason="checkpoint_lost"}` on "nothing pending" — the
  overwhelmingly common result for new turns and session reloads — which would drown the metric's real signal.
- **Evidence**: `app/agent/graph_hitl.py` docstrings; `tests/agent/test_durable_checkpoint.py::
  TestResumabilityErrorRejectsAnActivelyRunningThread`.

### R6. A new message during a pause is refused, never auto-cancelled

- **Decision**: `astream_events_turn` checks `paused_approval_async` first: resumable ⇒ an `error` with
  `ErrorCode.PENDING_APPROVAL` and `details={"tool_calls": [...]}`; not resumable ⇒ proceed with a
  `system_note`.
- **Rationale**: LangGraph's `.astream(new_input)` restarts from `__start__` and never revisits an
  unresumed interrupt, leaving its `tool_calls` dangling forever (verified against a real paused thread).
  Auto-cancelling on the caller's behalf could discard a mutating action a caller who did not know it was
  pending (a second tab, a bot, a bare API call) never chose to drop. For an *incompatible* checkpoint,
  refusing would strand the thread permanently (cancel is refused for the same reason), so proceeding is the
  only path that makes progress — surfaced, not silent.
- **Evidence**: `app/agent/runtime_stream.py::astream_events_turn`. **Not covered by any test** (found by
  `/speckit-analyze`): `tests/agent/test_agent_pause_handling.py` exercises only the *unattended* helper with mocked
  streams, and nothing exercises `paused_approval_async`, the `PENDING_APPROVAL` refusal or the `system_note`
  path — task T050 in `tasks.md`.

### R7. Resume re-asserts identity

- **Decision**: `astream_events_resume(thread_id, approved, ctx)` takes `ctx` as an argument and rebuilds
  `config["configurable"]`.
- **Rationale**: Verified empirically that `config["configurable"]` does not persist across a resume, so
  whoever resolves the approval must re-assert identity. The code notes "a stricter check — e.g. resuming
  principal must match the pausing tenant — would go here too"; it is **not built**, which is how feature
  002's B2 and an approved write running under the *resumer's* identity arose (B2 was closed in #67 by an owner check at the API, not by this check).
- **Evidence**: `app/agent/runtime_stream.py::astream_events_resume`.

### R8. Cancel has two mechanisms for two states

- **Decision**: *Streaming* turn → `POST /chat/cancel` sets `agent:cancel:<thread_id>` (TTL 60 s); the worker
  polls it between graph events via `_iterate_with_timeout(cancel_check=…)` and raises `TurnCancelled` →
  `error{code:"cancelled"}`. *Paused* turn → a `"cancel"` job calls `cancel_run`, which resumes with the
  sentinel. Both fire unconditionally from the one endpoint (each is a no-op if it does not apply).
- **Rationale**: A streaming turn has no queued job to target; a paused one has no running worker to poll.
  Cooperative cancel takes effect only at the next event boundary and does not interrupt a tool already
  running. `/chat/cancel` is excluded from rate limiting ("stopping a runaway turn must never itself be
  throttled").
- **Gap A12 (by reading; fixed in #65)**: `_process_turn` and `_process_turn_continue` clear any stale flag and pass a
  `cancel_check`; `_process_resume` does neither, and `astream_events_resume(thread_id, approved, ctx)` has no
  cancel-check parameter. A turn streaming *after* an approval therefore ignores the flag, and the cancel job
  then finds nothing paused. Clearing a stale flag at the start of a turn is deliberate (a flag nobody consumed
  must not cancel a later turn) and has the converse effect that a cancel arriving *before* a queued job starts is
  discarded.
- **Evidence**: `app/api/main.py::chat_cancel`; `app/job_queue/queue.py::set_cancel_flag`;
  `app/job_queue/agent_worker.py::_process_turn/_process_resume`; `app/agent/runtime_stream.py`;
  `tests/agent/test_cancellation.py`.

### R9. Unattended callers decline; scheduled jobs bypass the loop; sub-assistants are read-only

- **Decision**: (a) `astream_events_turn_unattended` swallows `approval_required` and resumes once with
  `False`, counting `agent_unattended_pause_total`. (b) A job that must write calls the domain's `_impl`/store
  functions directly (`scripts/ops_digest.py`: "build_graph() would hit should_continue's mandatory,
  UNCONDITIONAL gate … so this calls the ops domain's `_impl` directly"). (c) Sub-assistant catalogs keep only
  tools whose tier is `read_only`; anything unknown is treated as `outward` and dropped with a warning —
  "never upgraded" — and `run_subagent` is excluded so a sub-assistant cannot spawn another.
- **Rationale**: The gate has no unattended bypass by design; the only alternatives for a caller with no
  human are "decline" or "do not use the loop", never "approve".
- **Evidence**: `app/agent/runtime_stream.py`; `app/agent/subagent_tools.py`; `scripts/ops_digest.py`,
  `scripts/followup_sweep.py`; pattern 46/47. `scripts/ops_investigate.py` is a deliberate honest limit: a
  gated call in its one-shot graph simply ends with an empty answer and no write.

### R10. FINDING B4 — one-round auto-decline can strand a conversation *(fixed in #63)*

- **What was found**: the helper declines the *first* pause and then forwards every event of the resume
  stream, including a **second** `approval_required` if the model re-requests the gated action. The chat-app
  channel's `_run_turn` ignores that event type, so (1) the user gets an empty reply (`_send_message` sends
  nothing for empty text), contradicting the channel's documented "gets a real reply explaining it wasn't
  approved, never a silent write"; and (2) the thread stays paused, so every later message is refused with
  "pending approval — approve, reject, or cancel it", which that channel cannot do.
- **Evidence level**: *Reproduced* with the real `app.channels.telegram._run_turn`, an in-memory graph and a
  scripted model that requests `add_note` twice: message 1 → reply text `''` and the thread paused at
  `add_note`; message 2 → the pending-approval refusal. *Not reproduced* with a real model or a real chat-app
  connection. The precondition (a model re-requesting after a decline) is model behavior, but the stuck state
  is system behavior. The helper's docstring ("a second pause on the same turn is left to the model's own next
  response") does not match what it does.
- **Safety**: fails closed — no write occurs.
- **Candidate fixes (1)+(2) were built in #63**: (1) loop the decline while the stream ends on `approval_required`, with a
  small round cap; (2) when the stream ends on a pause the caller cannot resolve, auto-**cancel** the run
  (`cancel_run`) so the thread is never left paused, and reply with an explicit "that action needs approval and
  wasn't approved" message; (3) make the channel itself detect an `approval_required` and say so. (1)+(2) is
  the structural fix; the failing test scripts a model that requests the write twice.

---

## Part B — Exactly-once

### R11. The failures arrived in rounds, each invisible to the previous keying

The `GRAPH_PATTERNS.md` "Extending Further" log is the history of this feature:

| Round | Gap found | Fix |
|-------|-----------|-----|
| 0 | A crashed worker leaves a job pending forever | `XAUTOCLAIM` reclaim loop; never redeliver blindly |
| 1 | A reclaimed `"resume"` re-invokes the tool calls already pending under the **same** call ids | `idempotent()` + `tool_call_dedup` (migration 13); resume now always safe |
| 1b | A reclaimed `"turn"` restarts the LLM, minting **new** call ids no id-keyed defense can see | `_is_safe_to_retry_turn` scanned for mutating calls — **since removed** (see R17 and finding A11: three doc/comment references to it remain) |
| 2 | The narrow `result IS NULL` window lets a second run through; a duplicate **row** results | `tool_call_id UNIQUE … ON CONFLICT DO NOTHING` (migration 14); `uuid5` point ids |
| 2b | An append to a TEXT column has no row to put `ON CONFLICT` on; a replay doubled text | appends become rows (migration 15), `STRING_AGG` at read |
| 3 | A **soft timeout** after the write committed → the agent retries under a **new** id | `MutatingToolTimedOut` + "verify, don't retry" steering |
| 4 | Whole-turn restart of a turn that already ran a write | **continue** the checkpointed run (`astream_events(None, …)`) instead of restarting |
| 5 | Double-submit after the first job finished races nothing and runs a second turn | submission dedup + compensating `release_submission_claim`; results stream no longer deleted eagerly |

The lesson the constitution encodes: *each audit round found a window the previous keying could not see, so
a new write path states its duplicate story up front.*

### R12. `idempotent()` — an atomic claim keyed by the provider's call id

- **Decision**: `INSERT INTO tool_call_dedup (tool_call_id, tenant, thread_id, tool_name) VALUES … ON CONFLICT
  (tool_call_id) DO NOTHING RETURNING tool_call_id`. A returned row ⇒ this caller owns running `fn`; none ⇒
  read back `result`. After `fn` succeeds, `UPDATE … SET result`.
- **Rationale**: The provider assigns a fresh id per tool call it decides to make, so two invocations sharing
  one are, by construction, the same logical invocation. The primary key makes the claim atomic with no
  separate locking. `tenant`, `thread_id`, `tool_name` are observability metadata only.
- **Accepted race**: if the row read back has `result IS NULL` (the winner is still running or died before the
  closing `UPDATE`), the caller runs `fn()` itself rather than blocking. Accepted because nothing today can
  place two callers there concurrently: reclaim retries only after `AGENT_WORKER_RECLAIM_IDLE_SECONDS` (240 s)
  of the original going silent, and `acquire_thread_lock` already rules out two jobs on one thread. A future
  genuinely-concurrent caller would need block-and-poll or `SELECT … FOR UPDATE`.
- **Alternatives considered**: a Redis lock (extra dependency on the write path); keying on a business key
  (a product decision, deliberately not built).
- **Evidence**: `app/agent/tool_idempotency.py`; `postgres-init/13-tool-call-dedup.sql`;
  `tests/agent/test_tool_idempotency.py::TestIdempotent`.

### R13. Fails *open* on its own storage failure

- **Decision**: any exception from the claim → log `tool_call_dedup_degraded`, `agent_tool_dedup_degraded_total`
  +1, run `fn()` unprotected. A failure to *store the result* after success is counted and swallowed.
- **Rationale**: Layer one is defense in depth for a rare compounding failure (a crash-recovery replay landing
  exactly while the dedup store is also down); making it a precondition would let a dedup outage block every
  write in the app. The cost is a visible window, so `ToolCallDedupDegraded` alerts after 15 minutes.
- **Evidence**: `app/agent/tool_idempotency.py`; `observability/prometheus/alerts.yml`.

### R14. Layer two at the target

- **Decision**: pure INSERTs (`create_ticket`, `log_incident`, `add_followup`) carry a nullable
  `tool_call_id TEXT UNIQUE` with `ON CONFLICT (tool_call_id) DO NOTHING RETURNING id`; on no row, `SELECT` the
  existing row and return *its* id. Appends (`add_comment`, `append_lead_note`, `mark_lead_lost`'s reason,
  `log_lead_interaction`'s note) are rows in `support_ticket_comments` / `crm_lead_notes` keyed the same way;
  the flattened text is computed at read time with `STRING_AGG(… ORDER BY created_at)` over a `LEFT JOIN` so a
  parent with zero notes still returns a row. Vector writes use `uuid5(namespace, tool_call_id)`; ingestion uses
  content-addressed ids.
- **Rationale**: Without a constraint on the *target*, the accepted race in R12 becomes a real duplicate row.
  UNIQUE is nullable because rows written by anything other than the agent (a seed script, a future admin path)
  have no call id, and SQL treats every NULL as distinct. Not a general "same ticket twice" guard — a
  business-key rule is a product decision. `find_or_create_lead`'s upsert needed no key at all: `ON CONFLICT
  (tenant, contact) DO UPDATE SET updated_at = now()` is naturally idempotent.
- **Residual**: the store functions take `tool_call_id: str | None = None`; a caller that omits it gets no
  row-level protection.
- **Evidence**: `postgres-init/14-…`, `15-…`; `app/domains/*/store.py`; `.claude/rules/side-effect-tools.md`.

### R15. A soft timeout is a *different* gap — steer to verify

- **Decision**: `idempotent()` re-raises the `TimeoutError` from a write's own `_arun_with_timeout` as
  `MutatingToolTimedOut`; `_friendly_tool_error` dispatches on the exception *type* (ToolNode passes it only the
  exception, never the call) and tells the agent the effect "may already have been applied", **not** to call it
  again, and to check with a read-only status/list tool first.
- **Rationale**: `asyncio.wait_for` cancels the *awaiting* task, not necessarily what it is awaiting, so the
  write can commit on the far side of a slow store even though the call reports failure. The previous generic
  message ("Try a different approach") was *actively dangerous* here: "try again" mints a brand-new call id that
  nothing id-keyed can recognize. The steering is the one general defense that works for every tool without a
  per-tool business key.
- **Evidence**: `app/agent/tool_idempotency.py::MutatingToolTimedOut`; `app/agent/graph_utils.py::
  _friendly_tool_error`; `tests/agent/test_graph_utils.py`.

### R16. Automatic retries only where "never landed" is provable

- **Decision**: `CircuitBreaker.call(…, retry_on=…)` retries only exceptions the caller names. It wraps (a) the
  **read-only** sandbox tool-catalog listing (`retry_on=(ConnectionError, TimeoutError)`) and (b) the crawler's
  `Crawl4aiConnectionError` (a connect failure, so the request never landed). It never wraps a write; a
  half-open breaker admits exactly one trial under the lock.
- **Rationale**: A bare `except Exception` retry around a non-idempotent call is forbidden — a retry after a
  timeout arrives under a new id. Retrying a *listing* on `TimeoutError` is safe because it has no side effect.
- **Evidence**: `app/core/resilience.py`; `app/domains/sandbox_tools.py`; `app/ingestion/web_crawler.py`;
  `.claude/rules/runtime-reliability.md`.

### R17. Crash recovery *continues* a turn

- **Decision**: `agent_worker._classify_reclaimed_turn` inspects the checkpoint (read-only) and returns
  `retry_fresh` (no `HumanMessage` ever saved), `dead_letter` (unreadable; the turn already produced its final
  answer; or paused at a real interrupt), or `retry_continue` (anything else). `retry_continue` is republished as
  a `"turn_continue"` job that runs `graph.astream_events(None, config)`.
- **Rationale**: Verified against the installed `langgraph==0.2.76`'s `pregel/loop.py::Loop._match_writes`: input
  `None` sets `is_resuming=True` and proceeds from the checkpoint's unfinished superstep, matching already-
  recorded task writes (persisted per task in Postgres) instead of re-executing them — the same mechanism that
  makes a retried resume safe. Passing real input hits `Loop._first`'s other branch, which discards unfinished
  writes and starts a fresh superstep sequence: a brand-new `agent` call with brand-new call ids. Continuing closes the
  gap *structurally* rather than by scanning which tools are dangerous to repeat. A turn that already produced a
  final answer is not retried because retrying would only double-record its cost. `"resume"` and `"cancel"` jobs
  are always safe (a resumed call that already ran returns its cached result).
- **Known conservative edge**: if the crash came so early the turn's own `HumanMessage` was never saved, the last
  `HumanMessage` found belongs to the preceding, completed turn, so it reports `dead_letter` — a false negative,
  never a false positive.
- **Alternatives considered**: restart + dedup (cannot catch new ids); heuristically scan for mutating calls
  (the superseded round 1b).
- **Evidence**: `app/job_queue/agent_worker.py`; `app/agent/runtime_stream.py::astream_events_continue_turn`;
  `tests/job_queue/test_agent_worker.py::TestClassifyReclaimedTurn`, `TestHandleReclaimedJob`.

### R18. One active job per conversation; the lock is released *before* the terminal event

- **Decision**: `acquire_thread_lock` (`SET NX EX`, `THREAD_LOCK_TTL_SECONDS = 2 × turn timeout`) around every job
  kind; a loser fails fast with `THREAD_BUSY`; release is a Lua compare-and-delete; the handler releases **before**
  publishing the terminal event.
- **Rationale**: The consumer group guarantees no one request is delivered twice but says nothing about two
  *different* requests (a double-submit, a retry, a resume racing a new turn) on one thread: both read the
  checkpointer's latest state as parent and both write a child — last commit wins and the other turn vanishes.
  Release ordering was *measured* against a real 40-way concurrent pause/resume load: releasing in `finally`
  rejected ~1/3 of resumes as busy; releasing after `xadd` but before the TTL refresh still ~1/8; only releasing
  before `publish_result` at all closes the window.
- **Evidence**: `app/job_queue/queue.py`, `agent_worker.py::process_request`;
  `tests/integration/test_worker_scaling.py`; `tests/job_queue/test_agent_worker.py::
  TestSameThreadJobsAreSerialized`.

### R19. Submission dedup, its compensation, and a deadline on the first event

- **Decision**: `claim_or_get_existing_submission` keyed on `(thread_id, sha256([message, images]))` for
  `CHAT_SUBMIT_DEDUP_TTL_SECONDS = 10`; `release_submission_claim` if publishing then fails; `read_results(
  first_event_deadline_seconds=30)`; the results stream is **not** deleted when a reader sees the terminal event.
- **Rationale (each a real bug)**: the thread lock alone only rules out *concurrent* jobs — a retry after the first
  finished raced nothing and ran as a second, independent turn with fresh call ids. A claim that won but whose
  publish failed left retries streaming a stream nobody would write to. An eager `DEL` assumed one reader per
  `request_id`; once dedup let two callers share one, the first terminal event deleted the stream under the second
  (the hermetic test that caught it hung). `if not response: continue` looped forever when no worker existed.
- **Evidence**: `app/api/main.py::chat_stream_queued`, `_queued_sse_response`; `queue.py`.

### R20. Ack always — even on failure

- **Decision**: `process_request` acks in `finally`; a failed handler publishes an `error` and is **not**
  redelivered.
- **Rationale**: "a redelivered, already-attempted request would re-run the same side-effecting tool calls twice".
  Delivery is at-least-once for a *crashed* worker (no ack) and at-most-once for a *failed* handler. The error text
  here is the raw `str(exc)` — feature 001 advisory A2.
- **Evidence**: `app/job_queue/agent_worker.py::process_request`.

### R21. A retention sweep, run by hand

- **Decision**: `sweep_stale_rows(older_than_hours)` deletes `tool_call_dedup` rows older than the window;
  `scripts/tool_call_dedup_sweep.py` (`make tool-call-dedup-sweep`, default 24 h) is "meant for real cron" — the
  same "fixed pipeline, not an agent turn" shape as `followup_sweep.py`.
- **Rationale**: The table has no retention of its own; every write call's full result text accumulates forever
  otherwise. Rows need only outlive `AGENT_WORKER_RECLAIM_IDLE_SECONDS`.
- **Consequence**: nothing schedules it (plan A9).
- **Evidence**: `app/agent/tool_idempotency.py::sweep_stale_rows`; `tests/scripts/test_tool_call_dedup_sweep.py`.

### R22. A best-effort notification is a saga pivot

- **Decision**: `notify.post_to_team_channel` never raises; it appends to `var/team_channel.log` and, if
  configured, POSTs a webhook; every outcome is counted `agent_team_channel_notify_total{sink,outcome}` and a
  sustained failure alerts (`TeamChannelNotifyFailing`).
- **Rationale**: The preceding write already committed and is the source of truth (still pullable via a list/status
  tool); failing the tool because a push failed would invite a retry of a committed write.
- **Residual (R1)**: the message itself has no uniqueness key, so `escalate_to_human`, `handoff_to_human` and the
  ops `post_to_team_channel` tool are duplicate-safe only through layer one.
- **Evidence**: `app/domains/notify.py`; `app/domains/support/tools.py::_escalate_to_human_impl`;
  `app/domains/sales/tools.py::_handoff_to_human_impl`.

---

## Part C — Findings from verifying the as-built system

### R23. FINDING B3 — a cancelled marker that never clears *(fixed in #62)*

- **What was found**: `human_approval` returns `{"messages": …, "approved": False, "cancelled": True}` on a cancel.
  `validate_input` resets ~20 per-turn `State` fields but **not** `cancelled` (nor `approved`), and no other node
  clears it. `route_after_approval` tests `state.get("cancelled")` **first**, so on any later pause in the same
  thread — even one that is approved — it returns `__end__` before looking at `approved`.
- **Evidence level**: *Reproduced* with a fake model and an in-memory checkpointer: turn 1 paused and was
  cancelled (`state.cancelled = True`); turn 2 on the same thread paused, was **approved** (`Command(resume=True)`),
  and the run finished at once — `state.cancelled` still `True`, **no** `ToolMessage` for the approved call, the last
  message an `AIMessage` with pending `tool_calls` and empty content. *By reading, not reproduced*: with a real
  provider the dangling tool request would fail the next model call's validation.
- **Safety**: fails closed — the approved action does not run — but contradicts the approver's decision and leaves a
  dangling tool call in history.
- **Why tests missed it**: `test_cancelled_tool_call_ends_the_turn_without_reaching_agent_again` and
  `TestRouteAfterApproval` assert the cancel path and the routing function in isolation; none runs a *second*
  approval on the same thread.
- **Fix (built in #62)**: reset `cancelled` (and `approved`) in `validate_input` — **not** on a resume, since a resume
  must see the state the pause left. Failing test first: cancel on turn 1, approve on turn 2 of the same thread,
  assert the `ToolMessage` for the approved call exists and `cancelled` is `False` at the start of turn 2.

### R24. FINDING E2 — the dedup lookup ignores tenant

`_claim_or_cached_result` reads `SELECT result FROM tool_call_dedup WHERE tool_call_id = %s`. The row stores
`tenant`, but it is "denormalized observability metadata only … never read by `idempotent()`'s own correctness
logic". With globally unique provider ids this is harmless; a collision (including a test or fake model that
reuses ids like `call_1`) would return another tenant's stored result text. The conservative fix
(`AND tenant = %s`) changes a collision from "return the cached result" to "run the write", so it needs a
deliberate decision.

### R25. FINDING A7 — nothing pins the wrapper on every write tool *(closed in #68 by `tests/domains/test_write_tools_contract.py`)*

A script matched every non-`read_only` name in the four `TOOL_CAPABILITIES` mappings against
`idempotent(tool_name=…)` call sites: all 15 are wrapped, and so are the four sandbox tools in each of ops, sales
and support. Searching `tests/` for a test that would fail if a wrapper were removed found only tests of the wrapper
itself and of the *default* tool set's declarations (`test_every_tool_in_TOOLS_has_a_declared_capability`); the domain
tool modules have no tool-level tests of "refuses without ctx" or "routes through `idempotent()`", although
`.claude/rules/side-effect-tools.md` asks for them. A single generic test over every domain plugin would close it.

### R27. FINDING A11 — documentation still describes a removed safety check *(fixed in #66)*

`_is_safe_to_retry_turn` no longer exists in `app/` (only `_classify_reclaimed_turn` and `_turn_already_completed`
do), yet `GRAPH_PATTERNS.md` "Extending Further" (three entries, around lines 504, 505 and 509) still says a
retried `"turn"` is safe "only if its checkpointed state shows no `mutating`/`outward` tool call completed", and
comments in `app/core/metrics.py` (the `agent_worker_job_reclaimed_total` description) and `app/core/config.py`
(`max_auto_reclaim_retries`) name the removed function. The current behavior is the opposite of "refuse a turn that
ran a write": such a turn is **continued** (R17). Constitution Principle VIII requires conflicting documents to be
corrected; a reader following the doc would believe a turn that ran a write is dead-lettered, and would
misunderstand the metric.

### R26. FINDING A10 — approvals are unattributed

`human_approval` increments `agent_human_approval_total{decision}` and nothing else: no row records who decided,
when, or for which tool calls. Together with R7
(an approved write runs under the resumer's ctx; feature 002's B2 — the resumer was not verified as the owner — was fixed in #67), the gate controls *whether* a write happens but leaves no
auditable trail of *who allowed it*.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B3 | **Done — #62** (reset per turn, not on resume; same-thread cancel-then-approve regression test) | — |
| B4 | **Done — #63** (decline loop with a round cap, then cancel and an explicit reply) | — |
| A7 | **Done — #68** (`tests/domains/test_write_tools_contract.py`) | — |
| E2 | Tenant-scope the dedup lookup (or document the uniqueness assumption in the migration) | Policy decision on collision semantics |
| R1 | Send-once key for team-channel messages, or disclose the window | Needs a store and a product decision |
| A8 | Alerts for a rising unattended-pause rate | Unblocked by #63 |
| A9 | Schedule or document the dedup sweep; table-size gauge | Deployment-specific |
| A10 | Approvals audit record | Retention/PII decision |
| A11 | **Done — #66** | — |
| A12 | **Done — #65** | — |
| — | Integration test of the target-level `UNIQUE`/`ON CONFLICT` constraints (consumer-group delivery and the thread lock already have real-Redis tests) | Constitution VII known gap; needs Docker |
| A13 | Integration test of the real `XAUTOCLAIM` reclaim path (`reclaim_stale_entries`, incl. pagination) — today only a fake that differs from real Redis | Needs Docker; small |
