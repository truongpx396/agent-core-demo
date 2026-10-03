---

description: "Task list for feature 001 — Core RAG Agent Turn Pipeline (retrospective)"
---

# Tasks: Core RAG Agent Turn Pipeline

**Input**: Design documents from `/specs/001-core-rag-agent-turn/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Constitution Principle VII requires a regression test for every bug fix and
tests at the cheapest tier that can prove a behavior, so test tasks are not optional in this repo.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-02; the path named is where it lives.
  Nothing `[x]` needs doing — it records *what exists and where*, so a reviewer can trace every
  requirement to code and a test.
- **`[ ]`** = a **disclosed gap that is not built**. Each one is a real, separately mergeable
  change. Where it fixes a bug, the **failing test comes first** (CLAUDE.md working rules): write
  it, watch it fail on current code, then fix.
- Open task ids: **D1** constitution deviation, **A1** advisory, **A2** advisory (partly — the catch-alls were fixed in #64), **G3** gap
  (see plan.md *Complexity Tracking* and research.md *Deferred*). **Closed since this file was written:** B1 (#62),
  A2's catch-alls (#64), A3 (#68, #69).
- Paths are repo-relative. `tests/...` paths for `integration`/`llm` tiers need Docker/a model.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US7 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project skeleton, pinned dependencies, quality gates, local stack.

- [x] T001 Python 3.13 package skeleton `app/{agent,api,core,retrieval}/` and image `python:3.13-slim` in `Dockerfile`
- [x] T002 [P] Pin runtime deps with a reason comment on every non-obvious pin in `requirements.txt`; machine-generated `requirements-lock.txt`; dev tools in `requirements-dev.txt`
- [x] T003 [P] Lint/type gates in `pyproject.toml` (ruff F, I, UP, B, BLE, S110; mypy over `app/` and `scripts/`) and Makefile targets `lint`, `typecheck`, `test` in `Makefile`
- [x] T004 [P] Test tiers: markers `integration|llm|e2e|deepeval|crawl|sandbox` and the default `-m "not …"` that keeps a bare `pytest -q` hermetic, in `pyproject.toml`
- [x] T005 [P] Local stack: services `litellm`, `qdrant`, `postgres`, `redis`, `ml-service` in `docker-compose.yml`; aliases `chat`/`embed`/`vision` in `litellm-config.yaml`; load-bearing parallel-tool-call fix in `litellm-patches/sitecustomize.py`
- [x] T006 [P] CI pipeline running lint, typecheck, `make test`, integration and live tiers in `.github/workflows/ci.yml`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Everything every user story needs. **No story work until this phase is complete.**

- [x] T007 Typed settings with module-level re-exports for every tunable (budgets, timeouts, cache, history) in `app/core/config.py`, each mirrored in `.env.example`
- [x] T008 [P] Canonical error envelope: `ErrorCode` registry, `ErrorEnvelope.to_dict()`, `TurnCancelled` in `app/core/errors.py`
- [x] T009 [P] Metrics facade over OpenTelemetry (`.labels().inc()`, explicit histogram buckets) in `app/core/metrics.py` and process-start-only `configure_telemetry` in `app/core/telemetry.py`
- [x] T010 [P] structlog JSON logging with `run_id`/request-id binding in `app/core/logging_config.py`
- [x] T011 [P] Credential scrubbing chokepoint (regex shapes + bound secret values; fails open) in `app/core/scrubbing.py`
- [x] T012 [P] Identity primitives `SecurityCtx`, `valid_ctx` in `app/core/security.py` (behavior owned by feature 002; only consumed here)
- [x] T013 [P] Lazy, lock-guarded pooled Postgres connections (`get_connection()` is a transaction) in `app/agent/sql_store.py`
- [x] T014 `State` TypedDict, `SYSTEM_PROMPT` (ctx-free constant), safety-budget constants, `GraphDeps`, `_assemble_shared_graph_parts`, `STATE_SCHEMA_VERSION = 1` in `app/agent/graph.py`
- [x] T015 [P] Node instrumentation `_instrumented` (start/complete/fail/**paused**, metadata only), `_friendly_tool_error`, `_make_llm` in `app/agent/graph_utils.py`
- [x] T016 Durable checkpointer: `init_graph_async` on the calling loop, `AsyncConnectionPool`, `Semaphore` workaround for langgraph#7259, `RECURSION_LIMIT = MAX_ITERATIONS*2+15` in `app/agent/runtime.py`; database creation in `postgres-init/05-checkpointer-db.sql`
- [x] T017 [P] Embedding, sparse-embedding and re-rank clients in `app/retrieval/embeddings.py`; the `ml-service` container (`/health`, `/rerank`, `/prompt-guard`) in `docker/ml-service/main.py`
- [x] T018 Graph registration — every node wrapped by `_instrumented` at registration, `agent` alone with `AGENT_RETRY_POLICY`, `tools` with `handle_tool_errors` — and all edges in `app/agent/graph_build.py`
- [x] T019 [P] Test harness: autouse guards `mock_search_docs`, `mock_semantic_cache`, `mock_ml_moderation`, `mock_appdata_postgres` and the shared `TEST_CTX` in `tests/conftest.py`; real-service containers in `tests/containers.py`

**Checkpoint**: Foundation ready — the graph compiles and a fake-LLM turn can run end to end.

---

## Phase 3: User Story 1 — Ask a question, get a streamed answer with real sources (Priority: P1) 🎯 MVP

**Goal**: A question produces a streamed, cited answer and a list of sources actually used.

**Independent Test**: One factual question → `token`s, then `citations` (every `[n]` in the text is in
`items`), then `done` (quickstart scenarios 1–2; Tier 1 `test_graph_integration.py`).

### Tests for User Story 1

- [x] T020 [P] [US1] Full-graph direct-answer, tool-loop and approval paths with a fake chat model in `tests/agent/test_graph_integration.py`
- [x] T021 [P] [US1] Hybrid search: collection targeting, IDF-modified sparse config, batched upserts, dense-only degrade, reranker score replaces the RRF score, `min_score` floor (a no-op when rerank is skipped or degrades) in `tests/retrieval/test_qdrant_store.py` (hermetic; it does **not** assert the tenant filter in each `Prefetch` — that *effect* is proven against a real Qdrant by `tests/agent/test_concurrent_turns.py::TestQdrantReadWriteUnderConcurrency`, `integration` tier)
- [x] T022 [P] [US1] One terminal event; `citations`/`followups` ordering; synthetic `token` for non-model nodes; sub-agent tokens filtered in `tests/agent/test_streaming_terminal_events.py`
- [x] T023 [P] [US1] Multimodal content helpers (`_build_human_content`, `_human_text`, `_human_has_content` — whitespace-only is empty, image-only is not) in `tests/agent/test_multimodal.py`
- [x] T024 [P] [US1] Tool contracts: `args_schema` bounds, calculator allow-list, scrubbed results, per-tool row cap in `tests/agent/test_tools.py`

### Implementation for User Story 1

- [x] T025 [P] [US1] `hybrid_search` — dense + BM25 (`Modifier.IDF`) prefetch each carrying the tenant filter, server-side RRF, cross-encoder re-rank, `min_score` floor, degrade to dense-only / fused order with `agent_retrieval_degraded_total{stage}` in `app/retrieval/qdrant_store.py`
- [x] T026 [P] [US1] Tools `search_docs`, `calculator` (AST allow-list, never `eval`), `ask_clarification` with explicit Pydantic `args_schema`; `MIN_RERANK_SCORE = -8.0`; `_citation_records`/`_format_cited_context`/`_dedupe_by_parent`; `TOOL_RESULT_CAPS = {"query_employees": 20}` in `app/agent/tools.py`
- [x] T027 [US1] `retrieve_context` node (degrades to empty context, never fails the turn) in `app/agent/graph_retrieval.py`; vague-follow-up enrichment `_retrieval_query` and `_last/_previous_human_message` in `app/agent/graph_messages.py`
- [x] T028 [US1] `agent` node — splices `history_summary` and `<retrieved_document>` context at the fixed `context_anchor_index`, tail reminders, records tokens/cost from `usage_metadata` in `app/agent/graph_agent_node.py`
- [x] T029 [P] [US1] Citation computation: `_used_citations`, `_ungrounded_claims_count`, uncited/misattributed detection, `_insert_missing_citation_markers` in `app/agent/graph_citations.py`
- [x] T030 [P] [US1] `suggest_followups` (skipped when there are no citations) in `app/agent/graph_followups.py`
- [x] T031 [US1] Event translation — `astream_events` v2 → `token|tool_start|tool_end|retry|compacted|citations|followups|done|error`, `agent`-node-only tokens, exactly one terminal event; `astream_events_turn`; `_build_human_content` in `app/agent/runtime_stream.py`
- [x] T032 [US1] `POST /chat/stream/queued` request/response, identity-header dependencies (422 when absent), submission de-dup, SSE relay in `app/api/main.py` and `ChatRequest` in `app/api/schemas.py` *(queue/worker transport itself is outside this spec batch)*
- [x] T033 [P] [US1] Golden-set release gate — 5 repetitions per case, ≥80% repetition pass, ≥95% real citation markers — in `scripts/eval.py` and `tests/scripts/test_eval.py` (**SC-004**)

**Checkpoint**: US1 delivers a usable, cited, streaming assistant on its own.

---

## Phase 4: User Story 2 — Bad or unsafe input is stopped early and cheaply (Priority: P1)

**Goal**: No identity / empty message / injection ⇒ a fixed refusal and no answer-generation spend.

**Independent Test**: Quickstart scenarios 3–5 (empty, injection phrase, missing header) and Tier 1
`test_routing.py`, `test_moderation.py`.

### Tests for User Story 2

- [x] T034 [P] [US2] Identity checked before empty-input; `reject_context` increments `agent_missing_ctx_total` in `tests/agent/test_routing.py` (`TestRouteAfterValidation`, `TestRouteAfterValidationCtx`) and `tests/agent/test_graph_integration.py::TestRejectPath`
- [x] T035 [P] [US2] Pattern layer (singular/plural/dropped-article variants), classifier layer, fail-open on its own error, separate counter for classifier outage in `tests/agent/test_moderation.py`; routing in `tests/agent/test_routing.py::TestRouteAfterModeration`

### Implementation for User Story 2

- [x] T036 [US2] `validate_input` (stamps `ctx` once, resets per-turn fields), `route_after_validation`, `reject_input`, `reject_context` in `app/agent/graph.py`
- [x] T037 [P] [US2] Two-layer screen — injection/denylist regexes, Prompt Guard 2 via `ml-service` (threshold 0.5), fail-closed on match, fail-open + `agent_moderation_ml_degraded_total` on classifier outage — in `app/agent/moderation.py`
- [x] T038 [US2] `moderate_input`, `route_after_moderation`, `reject_moderation` ("I can't help with that request.") in `app/agent/graph.py`

### Open follow-ups for User Story 2 (not built)

- [ ] T039 [US2] **D1 — write the failing test first**: in `tests/agent/test_graph_integration.py` add a test that builds the graph with a call-counting fake LLM and tiny `history_token_ceiling`/`history_token_floor`, seeds history past the ceiling, then sends a known injection phrase; assert the fake LLM is invoked **zero** times (today the compaction summary call runs first, so this fails). Constitution VI: "moderation MUST run before any retrieval or LLM spend."
- [ ] T040 [US2] **D1 — reorder entry nodes** to `validate_input → moderate_input → compact_history → check_semantic_cache`: change the return values of `route_after_validation`, `route_after_moderation` and the `Literal` of `route_after_compaction` in `app/agent/graph.py`; update `tests/agent/test_routing.py` (`TestRouteAfterValidation*`, `TestRouteAfterModeration`, `TestRouteAfterCompaction`). Accepted trade-off to state in the PR: a *blocked* turn no longer compacts history (it compacts on the next valid turn). Decide, with `tests/agent/test_durable_checkpoint.py` green, whether `STATE_SCHEMA_VERSION` needs a bump (reasoning to record: a thread paused at `human_approval` resumes inside that node and never re-enters the reordered entry nodes).
- [ ] T041 [US2] **D1 — docs**: update the topology in `specs/001-core-rag-agent-turn/plan.md` and `GRAPH_PATTERNS.md` *Graph Flow*, remove the exception paragraph in spec User Story 2, FR-003 and SC-003, delete the D1 row in plan *Complexity Tracking*, and update the `route_after_validation` docstring that argues for the old order
- [ ] T042 [P] [US2] **A1 — alert rule**: add `ModerationClassifierDegraded` (`expr: increase(agent_moderation_ml_degraded_total[15m]) > 0`, same annotation style as `RetrievalDegraded`) to `observability/prometheus/alerts.yml`; no `promtool` check exists in the repo today, so validate once by hand (`promtool check rules`) and say so in the PR

**Checkpoint**: US2 holds the constitution's literal wording once T039–T041 land.

---

## Phase 5: User Story 3 — Every turn is bounded and always ends (Priority: P1)

**Goal**: No model behavior can make a turn run away; every ceiling trips into a visible message.

**Independent Test**: Scripted looping / fan-out / repeating / runaway-usage models each end the turn
with one terminal outcome and a moved counter (quickstart Tier 1).

### Tests for User Story 3

- [x] T043 [P] [US3] Per-turn reset of counters, tool-call fan-out budget, token budget, sub-run spend folded into ceilings, tool timeout, recursion limit in `tests/agent/test_safety_budgets.py` (`TestPerTurnReset`, `TestToolCallBudget`, `TestTokenBudget`, `TestSubagentSpendBudget`, `TestToolTimeout`, `TestRecursionLimit`)
- [x] T044 [P] [US3] Ceiling order and values, no-progress fingerprinting, invalid-tool and skill-without-search rejection in `tests/agent/test_routing.py` (`TestShouldContinue*`, `TestNoProgressDetection`, `TestToolCallFingerprint`, `TestConsecutiveRepeatCount`, `TestUseSkillCalledWithoutSearch`)
- [x] T045 [P] [US3] Iteration cap with distinct expressions (so it is not masked by no-progress) in `tests/agent/test_graph_integration.py::TestIterationCap`
- [x] T046 [P] [US3] Scrubbing at the tool chokepoint in `tests/core/test_scrubbing.py` and `tests/agent/test_tools.py`

### Implementation for User Story 3

- [x] T047 [US3] `should_continue` — ceilings first (rounds ≥10, tokens ≥16 000, cost ≥ `max_cost_usd_per_turn`), then fan-out >5, invalid names, skill-without-search, 3 identical batches, then the capability gate, in `app/agent/graph_routing.py`
- [x] T048 [P] [US3] `_tool_call_fingerprint`, `_consecutive_repeat_count` (pure function of `messages`, current turn only), `_mandatory_gate_reason` in `app/agent/graph_loop_guards.py`
- [x] T049 [P] [US3] `too_many_tool_calls`, `invalid_tool_call`, `_reject_tool_calls` (one `ToolMessage` per pending call) in `app/agent/graph_tools.py`
- [x] T050 [US3] `no_answer` safety-net node that re-vets trailing content with `check_output` instead of trusting it in `app/agent/graph_retry.py::make_no_answer_fallback_node`
- [x] T051 [P] [US3] Soft per-tool timeout `TOOL_TIMEOUT_SECONDS = 15` and scrub-on-return in `app/agent/tools.py::_arun_with_timeout`
- [x] T052 [P] [US3] Whole-turn deadline `_iterate_with_timeout` (`REQUEST_TIMEOUT_SECONDS`, default 60) in `app/agent/runtime_stream.py`
- [x] T053 [P] [US3] Incremental cost bookkeeping against the shared price table `PRICE_PER_1K_TOKENS_USD` in `app/agent/graph_agent_node.py` and `app/agent/usage_ledger.py`
- [x] T054 [P] [US3] Counters for every ceiling (`agent_token_budget_exceeded_total`, `agent_cost_ceiling_exceeded_total`, `agent_no_progress_total`, `agent_tool_budget_exceeded_total`, `agent_invalid_tool_call_total`) in `app/core/metrics.py`

### Open follow-ups for User Story 3 (not built)

- [x] T055 [US3] **B1 — regression tests** (written first, confirmed failing on the old code; #62): graph-level tests in `tests/agent/test_safety_budgets.py` — `TestPerTurnResetThroughTheGraph` (`…subagent_spend_recorded_in_one_turn_does_not_survive_into_the_next`, `…parallel_subagent_entries_within_one_turn_still_accumulate`) and `TestConcatOrResetReducer` — asserting the state read back from the compiled graph across two turns, never `validate_input`'s return value. See quickstart *Scenario 6*
- [x] T056 [US3] **B1 — fix** (#62): a reset-aware reducer `_concat_or_reset` on `State.subagent_spend` in `app/agent/graph.py` (concatenate, or `None` resets; concurrent `run_subagent` writes stay race-free) and `validate_input` writes `None`; `STATE_SCHEMA_VERSION` not bumped (no field added or removed)
- [x] T057 [US3] **B1 — docs** (#62): the `State.subagent_spend` comment and `GRAPH_PATTERNS.md` patterns 10 and 46 corrected; spec *Known gaps* B1 moved to *Resolved since this spec was written*; plan row B1 moved likewise

**Checkpoint**: US3 fully matches FR-019 (T055–T057 landed in #62).

---

## Phase 6: User Story 4 — Poor answers are repaired or retried before the person sees them (Priority: P2)

**Goal**: A bounded repair/retry loop turns known small-model failures into an extra round, not a
user-visible defect.

**Independent Test**: Feed `check_output` one answer per defect and confirm the reason, one retry per
distinct reason, exhaustion on a repeat, and the `retry` discard event (Tier 1).

### Tests for User Story 4

- [x] T058 [P] [US4] Each reason, priority order, repeat counter and the retry feedback per reason, auto-insert, and the `no_answer` fallback's re-vetting of trailing content in `tests/agent/test_nodes.py`; routing in `tests/agent/test_routing.py::TestRouteAfterCheck`; the retry-**exhausted** trust rule end to end in `tests/agent/test_graph_integration.py` (the uncited-content fallback) and `tests/agent/test_streaming_terminal_events.py` (T059)
- [x] T059 [P] [US4] `retry` event clears streamed text; exhaustion replaces vs. keeps; auto-corrected marker replaces streamed text in `tests/agent/test_streaming_terminal_events.py` (`TestRetryEventClearsTheStream`, `TestRetryExhaustedReplacesAlreadyStreamedContent`, `TestRetryExhaustedTrustsAttributionOnlyFailures`, `TestCheckOutputCitationAutoCorrectionStreaming`)
- [x] T060 [P] [US4] Real-model prompt-injection-via-retrieval regression (a correct answer missing only `[1]` must survive exhaustion) in `tests/live/test_prompt_injection_via_retrieval.py` (`llm` tier)

### Implementation for User Story 4

- [x] T061 [P] [US4] Heuristics `_defers_instead_of_acting`, `_fabricates_tool_output`, `_leaks_system_prompt`, `_skipped_required_sandbox_after_skill`, `_strip_fabricated_reference_footer`, and the fixed-priority `_retry_reason` in `app/agent/graph_output_guardrails.py`
- [x] T062 [US4] `check_output` (recomputes every field from scratch each call) and `route_after_check` (`MAX_CONSECUTIVE_SAME_RETRY_REASON = 2`) in `app/agent/graph_routing.py`
- [x] T063 [US4] `retry_output` (feedback names the specific reason; never quotes a leak), `retry_exhausted` (`_TRUST_CONTENT_RETRY_REASONS = {"too_short","uncited"}`), `_no_answer_message` in `app/agent/graph_retry.py`
- [x] T064 [P] [US4] One counter per rejection reason (`agent_deferred_instead_of_acting_total`, `agent_fabricated_tool_output_total`, `agent_system_prompt_leak_total`, `agent_skipped_required_tool_total`, `agent_misattributed_citations_total`, `agent_citation_auto_inserted_total`, `agent_reference_footer_stripped_total`, `agent_retry_total`, `agent_retry_exhausted_total`) in `app/core/metrics.py`

**Checkpoint**: US4 verified independently of US1's happy path by the node-level tests.

---

## Phase 7: User Story 5 — Conversations survive restarts and long histories stay bounded (Priority: P2)

**Goal**: Durable, process-independent conversations; bounded history with a cumulative summary.

**Independent Test**: Run a turn, replace the process, confirm history intact; push past the ceiling and
confirm whole-turn trimming, summary accumulation, and the named over-budget outcome.

### Tests for User Story 5

- [x] T065 [P] [US5] Real `AsyncPostgresSaver`: durability across a "restart", async seeding is idempotent, resume refused when not paused / on schema mismatch / while still running — `tests/agent/test_durable_checkpoint.py` (`integration` tier)
- [x] T066 [P] [US5] History hysteresis, whole-turn trimming, summary fold, summarization failure still trims in `tests/agent/test_safety_budgets.py` (`TestHistoryBudget`, `TestCompactHistoryNode`) and `tests/agent/test_routing.py::TestRouteAfterCompaction`
- [x] T067 [P] [US5] Transcript replay incl. the compaction breadcrumb as `role: system` in `tests/agent/test_session_messages.py`
- [x] T068 [P] [US5] 250 concurrent queued turns on 5 real workers and a concurrent HITL pause/resume round trip in `tests/integration/test_worker_scaling.py` (`integration` tier; SC-005)

### Implementation for User Story 5

- [x] T069 [US5] `compact_history` — `_messages_to_trim` (ceiling 24 000 → floor 4 000 est. tokens via `tiktoken`, whole turns, never the system prompt or current turn), cumulative `history_summary`, `COMPACTION_MARKER_KEY` breadcrumb in `app/agent/graph_compaction.py`
- [x] T070 [US5] `route_after_compaction` + `context_window_exceeded` (summary > `MAX_HISTORY_SUMMARY_CHARS = 4000` ⇒ named terminal message, counter) in `app/agent/graph.py`
- [x] T071 [US5] `_ensure_seeded_async` (checks stored messages; `_seeded` is only a fast path) in `app/agent/runtime.py`
- [x] T072 [US5] `resumability_error_async`, `_resumability_error_from_state` (paused = `state.tasks[i].interrupts`, not `state.next`; schema mismatch only; a build-SHA difference alone is not an error), `paused_approval_async` in `app/agent/graph_hitl.py` *(approval behavior itself is feature 003)*
- [x] T073 [P] [US5] `get_session_messages` / `get_pending_approval` in `app/agent/runtime_stream.py`; session directory in `app/agent/sessions.py` with `postgres-init/06-chat-sessions.sql` and `postgres-init/11-chat-sessions-domain.sql` *(ownership/scoping semantics are feature 002)*

**Checkpoint**: US5 independently verifiable at the integration tier.

---

## Phase 8: User Story 6 — Dependency hiccups degrade the answer, not the turn (Priority: P2)

**Goal**: Each dependency failure follows its documented policy and moves a counter.

**Independent Test**: Inject a failure into each of the seven dependencies and confirm a completed turn
and the matching counter (SC-008).

### Tests for User Story 6

- [x] T074 [P] [US6] Pre-fetch failure degrades to empty context and counts it, vague-follow-up enrichment in `tests/agent/test_nodes.py`
- [x] T075 [P] [US6] Tool exception → natural-language message to the model in `tests/agent/test_graph_utils.py`; a **structural** check that the `agent` node carries `AGENT_RETRY_POLICY` in `tests/agent/test_graph_integration.py` (the retry *behavior* — 3 attempts on a transient error, none on a programming error — is **not** exercised by any test)
- [x] T076 [P] [US6] Envelope shape and `to_dict()` JSON-safety in `tests/core/test_errors.py`; node lifecycle logging incl. `node_paused` in `tests/agent/test_instrumentation.py`

### Implementation for User Story 6

- [x] T077 [P] [US6] `AGENT_RETRY_POLICY = RetryPolicy(max_attempts=3)` (default `retry_on`, so programming errors are not retried) in `app/agent/graph.py`; `handle_tool_errors=_friendly_tool_error` in `app/agent/graph_build.py`
- [x] T078 [P] [US6] `agent_context_retrieval_degraded_total` increment in `app/agent/graph_retrieval.py`; `agent_retrieval_degraded_total{stage}` in `app/retrieval/qdrant_store.py`; liveness/readiness (`/health`, `/health/ready`, five bounded checks) in `app/api/health.py`

### Open follow-ups for User Story 6 (not built)

- [x] T079 [US6] **A2 — failing tests first** (#64): `tests/agent/test_streaming_terminal_events.py::TestAGraphFailureNeverLeaksItsMessageToTheCaller` (a hostname-shaped sentinel must not appear anywhere in the serialized event; `code == "internal"`; `details == {"error_class": "RuntimeError"}`; a `TimeoutError` still reports `timeout`) and the updated worker tests in `tests/job_queue/test_agent_worker.py` (four had asserted the leak)
- [x] T080 [US6] **A2 — fix the catch-alls** (#64): `internal_error_envelope(exc)` in `app/core/errors.py` (fixed message + `details={"error_class": …}`, never `str(exc)`) used by `app/agent/runtime_stream.py::_run_graph_stream`, `app/agent/runtime_legacy_stream.py` and `app/job_queue/agent_worker.py::process_request`; the real text stays on the trace and the log line carries the class only. **Not covered**: the ingest worker's catch-all (feature 006, B10)
- [ ] T081 [P] [US6] **A2 — first-event deadline**: failing test in `tests/job_queue/test_queue.py`, then wrap the `read_results` deadline error in `app/job_queue/queue.py` in an envelope. Decide and record in `contracts/error-envelope.md` whether to reuse `ErrorCode.TIMEOUT` (with `details={"kind":"no_first_event"}`) or add a new member; keep `content` unchanged so existing clients keep working
- [ ] T082 [P] [US6] **A2 — refused resume**: failing test in `tests/agent/test_durable_checkpoint.py::TestResumabilityError`, then make `app/agent/runtime_stream.py::astream_events_resume` emit `ErrorCode.CHECKPOINT_LOST` / `CHECKPOINT_INCOMPATIBLE` (return the code from `app/agent/graph_hitl.py::_resumability_error_from_state` instead of parsing a text prefix); keep the `content` text and the trailing `done` so current clients are unaffected
- [ ] T083 [US6] **A2 — decide emit-or-retire** for the four never-emitted codes (`moderation_blocked`, `cost_ceiling_exceeded`, `no_progress`, `unattended_pause`): either emit them (changes the SSE contract — needs UI/CLI/Telegram review) or remove them from `app/core/errors.py`; update `tests/core/test_errors.py`, `contracts/error-envelope.md`, spec FR-033/*Known gaps*, and plan row **A2** to match

**Checkpoint**: after T079–T083 the envelope is universal and Principle V's wording is met.

---

## Phase 9: User Story 7 — A repeated question is answered without redoing the work (Priority: P3)

**Goal**: A near-identical earlier answer for the same caller returns immediately; failure is a miss.

**Independent Test**: Quickstart scenario 9; `test_routing.py::TestRouteAfterCache`.

### Tests for User Story 7

- [x] T084 [P] [US7] Cache hit streams the cached answer and still passes `check_output`; a hit is not re-written; follow-ups skipped on a hit in `tests/agent/test_streaming_terminal_events.py::TestSemanticCacheHitStreamsTheCachedAnswer`, `tests/agent/test_routing.py::TestRouteAfterCache`, `tests/agent/test_nodes.py`

### Implementation for User Story 7

- [x] T085 [P] [US7] Redis Stack KNN cache restricted to tenant **and** principal, `SEMANTIC_CACHE_SIMILARITY_THRESHOLD = 0.95`, `SEMANTIC_CACHE_TTL_SECONDS = 3600`, every failure a miss with `agent_semantic_cache_total{outcome}`, `_escape_tag` for RediSearch TAG syntax in `app/retrieval/semantic_cache.py`
- [x] T086 [US7] `check_semantic_cache` / `route_after_cache` (hit → `check_output`) and `write_semantic_cache` (after a confirmed-final, non-hit turn only) in `app/agent/graph_cache.py`; wiring in `app/agent/graph_build.py`

### Open follow-ups for User Story 7 (not built)

- [x] T087 [P] [US7] **A3 — the `_escape_tag` guarantee made explicit** (#68, #69): hermetic `tests/retrieval/test_semantic_cache.py` (tenant and principal scoping, fail-closed incomplete ctx), `tests/retrieval/test_semantic_cache_tag_escaping.py` and the real-Redis `tests/integration/test_semantic_cache_tag_escaping_real_redis.py`. Writing it exposed a real defect — `|` and `\` were not escaped, so a principal `alice|bob` built a filter that also matched `bob`'s entries — fixed in #69 by escaping every ASCII character that is not a letter, digit or underscore
- [ ] T088 [US7] **G3 — write the failing test first**: in `tests/agent/test_nodes.py` (or `test_graph_integration.py`) cache a normal answer for "what are the support hours?", then in a **new thread** of the same principal send the vague follow-up "pls be more detailed" whose cached counterpart came from an unrelated conversation; assert the cached answer is **not** served. Reproduce first — this gap was found by reading, not by running
- [ ] T089 [US7] **G3 — fix**: decide among (a) fold the same prior-question enrichment `app/agent/graph_messages.py::_retrieval_query` already uses into the cache key, symmetrically in `make_check_semantic_cache_node` and `make_write_semantic_cache_node` (`app/agent/graph_cache.py`); (b) skip the cache for messages `_retrieval_query` classes as vague; (c) key on `(message, thread_id)`. Record the choice (hit-rate vs. wrong-context risk) in `research.md` R16, then update spec *Known gaps*, `GRAPH_PATTERNS.md` pattern 22, and delete plan/research G3 rows

**Checkpoint**: US7 correct for context-dependent follow-ups once T088–T089 land.

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, gates and standing reminders.

- [x] T090 [P] Pattern entries with their motivating bugs — patterns 1–7, 10–14, 16, 19–20, 22, 25–26, 30–32, 34–35, 39–41 — in `GRAPH_PATTERNS.md`; architecture and make targets in `README.md`
- [x] T091 [P] Metric-backed alert rules (`HighTurnErrorRate`, `HighTurnLatencyP95`, `RetrievalDegraded`, `SemanticCacheErrors`, `CheckpointIssues`, `ModerationBlockSpike`, `RateLimitRejectionSpike`) in `observability/prometheus/alerts.yml`
- [x] T092 [P] Constitution ratified with the principles this feature is checked against in `.specify/memory/constitution.md`
- [x] T093 [P] Ran `/speckit-analyze` (read-only) over `spec.md`, `plan.md`, `tasks.md` on 2026-10-02 and reconciled what it found — see this feature's `checklists/requirements.md` *Validation iterations* for the findings, the corrections made, and the items deliberately left for a decision (requirement-id traceability tags; `promtool check rules` in CI)
- [ ] T094 Run `specs/001-core-rag-agent-turn/quickstart.md` Tier 2 and Tier 3 on a machine with Docker and a native Ollama (Tier 1: 324 passed on 2026-10-02, 335 on 2026-10-03 after #62–#69) and record the result in the PR that closes the open follow-ups
- [ ] T095 Standing reminder: before **any** `langgraph-checkpoint-postgres` bump, re-check the `Semaphore` lock workaround in `app/agent/runtime.py::_open_checkpointer` (it pokes a private attribute of `AsyncPostgresSaver`) and remove it once langchain-ai/langgraph#7269 ships (research G1)

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Setup; **blocks every user story**.
- **US1 (P1)** → needs Phase 2 only. It is the MVP.
- **US2 (P1)**, **US3 (P1)** → need Phase 2; US3's tests reuse US1's graph harness but not its code paths.
- **US4 (P2)** → needs US1 (it gates US1's candidate answer) and US3's `no_answer` node (T050).
- **US5 (P2)** → needs Phase 2 (checkpointer) and US1's `agent` node for the summary splice.
- **US6 (P2)** → cross-cutting; each policy lives in a node from US1/US2.
- **US7 (P3)** → needs US1 (retrieval/gate) and US4 (cache writes only after the gate).
- **Polish** → after the stories you want in the release.

### Open follow-ups — independence and PR boundaries

Each follow-up is its own PR (CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines):

| PR | Tasks | Touches | Depends on |
|----|-------|---------|------------|
| 1 | T055–T057 (B1) | **done — #62** | — |
| 2 | T079–T080 (A2, catch-all) | **done — #64** | — |
| 3 | T081–T083 (A2 remainder) | `queue.py`, `runtime_stream.py`, `graph_hitl.py`, `errors.py` | PR 2 (shares the envelope decision) |
| 4 | T039–T041 (D1) | `graph.py`, routing tests, docs | — |
| 5 | T042 (A1) | `alerts.yml` | — |
| 6 | T087 (A3) | **done — #68, #69** | — |
| 7 | T088–T089 (G3) | `graph_cache.py`, tests, docs | PR 6 recommended first |

PRs 1, 2, 4, 5 are mutually independent and can proceed in parallel.

### Within each story

Tests (where listed) → helpers/models → nodes → routing → streaming/HTTP integration. For every
`[ ]` bug task the failing test precedes the fix.

### Parallel opportunities

- All Setup tasks marked [P]; all Foundational tasks marked [P] (T008–T013, T015, T017, T019).
- After Phase 2, US1, US2 and US3 can be staffed in parallel.
- Within a story, every test task is [P]; implementation tasks on different files are [P].

## Parallel Example: User Story 1

```bash
# Tests together (different files):
Task: "T020 Full-graph paths in tests/agent/test_graph_integration.py"
Task: "T021 Hybrid search in tests/retrieval/test_qdrant_store.py"
Task: "T022 Terminal events in tests/agent/test_streaming_terminal_events.py"
# Implementation together:
Task: "T025 hybrid_search in app/retrieval/qdrant_store.py"
Task: "T026 Tools in app/agent/tools.py"
Task: "T029 Citations in app/agent/graph_citations.py"
```

## Implementation Strategy

### As-built order (what happened)

Setup → Foundational → US1 → US2/US3 (hardening) → US4 (quality gate, driven by live-trace failures)
→ US5 (durability, compaction) → US6 → US7. The repo's history shows the quality gate and budgets
were added reactively, each behind a real incident; `research.md` names them.

### Closing the open follow-ups (what to do next)

1. ~~PR 2 (T079–T080, the catch-all)~~ — done in #64.
2. ~~PR 1 (B1)~~ — done in #62.
3. **PR 4 / PR 5** (D1, A1): bring the code in line with the constitution's literal wording.
4. **PRs 3, 6, 7**: contract cleanup and the cache.
5. Re-run `quickstart.md`, then delete each resolved row from `plan.md` *Complexity Tracking*.

### MVP scope

User Story 1 alone (T001–T033) is the minimum viable assistant. It is **not** safe to expose without
US2 and US3 — those two are the smallest set that satisfies the constitution's bounded-failure and
input-screening principles, so the practical MVP is **US1 + US2 + US3**.

## Notes

- `[P]` = different files, no dependency on an incomplete task.
- `[x]` here means "present", not "re-verified today" — only Tier 1 (T094) was re-run — on
  2026-10-02 and again on 2026-10-03 after #62–#69. Tier 2/3 results are not claimed.
- Features 002 (isolation, memory) and 003 (approval, exactly-once) own several behaviors this
  feature merely routes through; tasks that touch them say so and do not restate them.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks.
