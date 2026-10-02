# Quickstart: Validate Approval and Exactly-Once Writes

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier first.
**Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves routing, the wrapper's logic, store statement *shape* and reclaim
> classification. It does **not** prove that a real `UNIQUE`/`ON CONFLICT` behaves, nor the real `XAUTOCLAIM`
> reclaim path (consumer-group *delivery* and the thread lock are tested against real Redis in Tier 2)
> — and it did **not** catch B3 or B4, which Scenarios B3 and B4 below reproduced. Both were fixed in #62 and #63; the scenarios are
> now the regression tests named in each (expected: they pass).

---

## Tier 1 — Hermetic (no services, ~7 s)

```bash
pytest tests/agent/test_routing.py tests/agent/test_tool_idempotency.py tests/agent/test_cancellation.py \
       tests/agent/test_agent_pause_handling.py tests/agent/test_graph_utils.py \
       tests/agent/test_graph_integration.py tests/agent/test_subagents.py \
       tests/job_queue tests/domains tests/scripts/test_tool_call_dedup_sweep.py tests/channels -q
```

**Expected** (observed 2026-10-03, after #62–#69): `465 passed` (was `400` on 2026-10-02).

| Requirement | Evidence |
|-------------|----------|
| FR-001/FR-002 tier gating, undeclared ⇒ gated, no bypass | `test_routing.py` (`TestToolCapability`, `TestMandatoryGateReason`, `TestShouldContinueMandatoryGate`) |
| FR-004 approve / reject / cancel, one message per pending call | `test_graph_integration.py::TestHumanApprovalPath`, `test_routing.py::TestRouteAfterApproval` |
| FR-005 invalid / over-large batches bounce before the gate | `test_routing.py::TestShouldContinueInvalidToolCall`, `TestShouldContinue` |
| FR-013 unattended auto-decline (mocked streams) | `test_agent_pause_handling.py` |
| FR-015 sub-assistants read-only, no recursion | `test_subagents.py` |
| FR-016/FR-017 wrapper: first call runs, replay returns the cached result, fails open and counts | `test_tool_idempotency.py::TestIdempotent` |
| FR-019 timeout ⇒ "verify, don't retry" message | `test_graph_utils.py` |
| FR-018 store statements carry `ON CONFLICT (tool_call_id)` and the tenant | `tests/domains/{support,ops,sales}/test_store.py` (fake cursors) |
| FR-022 retention sweep | `tests/scripts/test_tool_call_dedup_sweep.py` |
| FR-023–FR-028 job dispatch, reclaim classification, retry cap, thread serialization | `tests/job_queue/test_agent_worker.py`, `test_queue.py` |

## Tier 2 — Real Postgres / Redis (Docker, no model)

```bash
make test-integration
```

**Expected**: passes or self-skips if Docker is unreachable. Relevant: `tests/agent/test_durable_checkpoint.py`
(pause survives a "restart"; resume refused when not paused / on schema mismatch / while still running —
**FR-007/FR-008, SC-003**), `tests/integration/test_queue_real_redis.py`, and
`tests/integration/test_worker_scaling.py` (a concurrent HITL pause/resume round trip across 5 real worker
processes, writing to a real Qdrant — **SC-001/SC-003/SC-006**). *Not run while writing this spec.*
**Nothing here proves the target-level `UNIQUE` constraints or the real `XAUTOCLAIM` reclaim** (the worker subprocesses in `test_worker_scaling.py` do hit a real `tool_call_dedup`, but nothing asserts on it) — see the manual checks below.

## Scenario B3 — Confirm a cancelled conversation can approve a later action (hermetic; fixed in #62 — expected: it no longer reproduces)

In a scratch Python session (do not commit), with no services:

1. `build_graph(GraphDeps(llm=<a scripted model that returns two calculator tool calls then a final answer>,
   search_docs=<async no-op returning ("", [])>, cache_get=<async no-op returning None>, cache_set=<async
   no-op>))`.
2. Turn 1 on thread `T` with `{"messages": [HumanMessage("what is 2+2?")], "require_approval": True}`; confirm it
   paused; resume with `Command(resume=CANCEL_SENTINEL)`; read `state.values["cancelled"]` → `True`.
3. Turn 2 on the **same** `T` with a new question and `require_approval: True`; confirm it paused; resume with
   `Command(resume=True)`.
4. Read the final state.

**Observed 2026-10-02 (before #62)**: after step 3 the run is finished (`next == ()`), `cancelled` is still `True`, there is **no**
`ToolMessage` for the approved call, and the last message is the assistant's own tool request with empty content.
**Since #62**: step 3 produces the tool result and a final answer, and `cancelled` is `False` at the start of turn 2 —
`tests/agent/test_graph_integration.py::TestHumanApprovalPath::test_an_approval_on_a_later_turn_still_runs_after_an_earlier_cancel_on_the_same_thread`.

## Scenario B4 — Confirm an unattended conversation is not stranded by a second pause (hermetic; fixed in #63 — expected: it no longer reproduces)

1. Use the real `app.channels.telegram._run_turn`, a scripted model whose first two replies each request the
   mutating `add_note` tool (a third reply is a plain answer), an in-memory graph returned by a patched
   `runtime.init_graph_async`, and no-op patches for the tenant-budget, reservation, session-upsert and trace
   helpers.
2. Run message 1 (`"please save a note"`) for the thread `telegram:42`; then call `paused_approval_async` on it.
3. Run message 2 (`"hello? are you there?"`).

**Observed 2026-10-02 (before #63)**: message 1 → the channel's reply text is `''` (nothing would be sent) and the thread is
**paused at `add_note`**; message 2 → *"This conversation has a pending approval — approve, reject, or cancel it
before sending a new message."* — which the channel offers no way to do. **Since #63**: the thread is never left
paused and the user receives an explicit "that action needs approval and wasn't approved" reply —
`tests/agent/test_agent_pause_handling.py::TestUnattendedSecondPause`.

## Tier 3 — Full local stack, manual walk-through

**Prerequisites**: as feature 001 Tier 3 (`make up`, `make pull-models`, `make ingest`, `make serve`, and a worker
for the **support** domain: `make agent-worker-support`). Helper (a function — unquoted variables are not
word-split in zsh):

```bash
sup() { curl -N -X POST "localhost:8000/$1" -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: ecorp' -H 'X-Principal-Id: alice' -H 'X-Domain: support' -d "$2"; }
```

| # | Request | Expected | Proves |
|---|---------|----------|--------|
| 1 | `sup chat/stream/queued '{"message":"Open a ticket: my invoice total is wrong","thread_id":"ap-1"}'` | stream ends with `approval_required` naming `create_ticket` and its args; **no ticket exists yet** (check the ticket list/DB) | FR-002/FR-003 (model-dependent: rephrase if it does not call the tool) |
| 2 | `sup chat/stream/queued '{"message":"anything","thread_id":"ap-1"}'` | `error{code:"pending_approval", details:{tool_calls:[…]}}` | FR-010 |
| 3 | `curl localhost:8000/chat/sessions/ap-1/pending_approval -H 'X-Tenant-Id: ecorp' -H 'X-Principal-Id: alice' -H 'X-Domain: support'` | `{tool_calls:[…], resumable:true}` | FR-012 |
| 4 | restart `make serve` and the worker, repeat #3 | unchanged | SC-003 |
| 5 | `sup chat/resume '{"thread_id":"ap-1","approved":true}'` | the ticket is created once; stream ends `done` | FR-004 |
| 6 | repeat #5 | `error` beginning `checkpoint_lost: …` then `done`; **no second ticket** | FR-008, FR-017 |
| 7 | new thread `ap-2`, same request as #1, then `sup chat/resume '{"thread_id":"ap-2","approved":false}'` | no ticket; the assistant acknowledges the rejection | FR-004 |
| 8 | new thread `ap-3`, as #1, then `sup chat/cancel '{"thread_id":"ap-3"}'` | stream ends `error{code:"cancelled"}`; no ticket; no further assistant text | FR-004, SC-005 |
| 9 | send the identical #1 request twice within 10 s on a fresh thread | one turn (check worker logs for one job) | FR-027 |
| 10 | two concurrent requests on one thread | the second gets `error{code:"thread_busy"}` | FR-026 |

### Replay safety (the manual check for the missing integration test)

With `psql` against the local `appdata` database (the `ecorp` tenant; do not use real data), after step 5 note the
ticket's `id` and `tool_call_id`. Then run the *same insert* twice by hand with that `tool_call_id` and confirm the
second affects 0 rows (`ON CONFLICT (tool_call_id) DO NOTHING`):

```sql
INSERT INTO support_tickets (tenant, requester, subject, description, priority, tool_call_id)
VALUES ('ecorp','alice','dup','dup','low','<the id from the first row>') ON CONFLICT (tool_call_id) DO NOTHING RETURNING id;
```

**Expected**: no row returned. This is the real-constraint behavior the hermetic tier cannot prove (Principle VII
known gap; task in `tasks.md`).

### Crash recovery (needs two terminals; do not run against real data)

Start a long turn that calls a write tool, kill the worker process (`kill -9`) after the ticket exists but before
the stream ends, start a worker again, and wait `AGENT_WORKER_RECLAIM_IDLE_SECONDS` (240 s default; set it lower in
`.env` for the experiment). **Expected**: the job is continued from its checkpoint (log `agent_worker_job_reclaimed`
with `outcome=retried`) or archived with `worker_lost` — and there is **one** ticket either way.

## Checking the alerts

`ToolCallDedupDegraded` and `TeamChannelNotifyFailing` are rule-file entries (`observability/prometheus/alerts.yml`);
there is no `promtool` check in the repo. To see `ToolCallDedupDegraded` fire in a scratch environment, stop the
`appdata` Postgres while a write tool runs (the write still runs unprotected and `agent_tool_dedup_degraded_total`
increments), then restart it.

## Troubleshooting

- *Step 1 returns an answer instead of `approval_required`*: the small model chose not to call the tool; rephrase
  ("Use create_ticket to open a ticket with subject …").
- *Step 5 reports `thread_busy`*: the original worker has not yet released the lock; retry in a moment (the lock is
  released *before* the terminal event, so this means a turn is genuinely still running).
- *Scenario B3/B4 do not reproduce*: expected — they were fixed in #62 and #63.
- *Do not* run `make clean`, `clear-*` or `restart-all` while validating.
