---

description: "Task list for feature 008 — Observability and Cost Governance (retrospective)"
---

# Tasks: Observability and Cost Governance

**Input**: Design documents from `/specs/008-observability-and-cost-governance/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principle VII requires a regression test for every bug fix. All six defects (B17–B22) were *missed by the existing tests*: the ledger and reservation are tested against fakes and fake cursors, the stream core is
tested for what the client sees but not for what is *recorded*, nothing measures a telemetry layer's own cost, and nothing validates the alert rules or the dashboards. The open test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-03; the path is where it lives. Nothing `[x]` needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written first** (CLAUDE.md working rules): write it, watch it fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **B17** a turn that does not complete records no usage · **B18** a tracing client and three threads per turn, plus one per flush · **B19** usage recording
  blocks the event loop · **B20** a leaked reservation is resurrected · **B21** a worker-pool outage is invisible · **B22** scheduled jobs never export metrics · **A1** degrade paths are log-only · **A2** pricing · **A3** ledger
  index and retention · **A4** unwatched metrics · **A5** label names unenforced · **A6** resume skips the allowance · **A7** no alert is delivered · **A8** configuration and comment drift · **A9** untested ledger statements,
  alert rules and dashboards · **A10** the constitution and the traces disagree.
- **No defect here weakens tenant isolation**: every ledger statement filters by tenant and the allowance is enforced before any model work. B17 and B20 are *accounting* defects; B21 and B22 are *visibility* defects; B18 and B19
  are *resource* defects. **A10 cannot be fixed by a code change alone** — it needs the constitution process.
- Tasks needing Docker say `docker` or `integration`. Paths are repo-relative. A path that does not exist yet is a file the task creates.

## Format: `[ID] [P?] [Story] Description *(requirements it serves)*`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US5 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] The metrics module — the `Counter`/`Histogram` wrapper over the OpenTelemetry API, 53 instruments, `MetricsCallbackHandler`, `_fingerprint` — in `app/core/metrics.py` *(FR-002, FR-003, FR-005, FR-007)*
- [x] T002 [P] The telemetry wiring — `configure_telemetry`, the OTLP push, the bucket Views — in `app/core/telemetry.py` *(FR-001, FR-002)*
- [x] T003 [P] Structured logging — `configure_logging`, `bind_request_id`, the JSON formatter — in `app/core/logging_config.py` *(FR-006)*
- [x] T004 [P] The two tables — `usage_ledger` (+ `resolved_model`) and `tenant_budget_reservations` — in `postgres-init/03-meter.sql`, `postgres-init/04-resolved-model.sql`, `postgres-init/12-tenant-budget-reservations.sql` *(FR-013, FR-019)*
- [x] T005 [P] The monitoring stack — collector, Prometheus, Alertmanager, Loki, Promtail, Grafana — in `docker-compose.observability.yml`, `docker-compose.observability.prod.yml` and `observability/` *(FR-001, FR-010)*
- [x] T006 [P] Settings `max_cost_usd_per_turn` (0.50), `max_cost_usd_per_tenant_per_day` (20.0), `request_timeout_seconds` (60), `otel_exporter_otlp_endpoint` in `app/core/config.py` (**three have no `.env.example` entry — A8**) *(FR-018)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The pieces every story uses.

- [x] T007 [P] The ledger module — `record_usage`, `usage_summary`, `reserve_budget`, `release_budget_reservation`, `in_flight_reservation`, `PRICE_PER_1K_TOKENS_USD` — in `app/agent/usage_ledger.py` *(FR-013, FR-014, FR-019, FR-020)*
- [x] T008 [P] The concrete-model resolver — `resolve_model`, cached on success only — in `app/agent/model_resolver.py` *(FR-013)*
- [x] T009 [P] The budget-exceeded error code and the envelope — in `app/core/errors.py` and `app/agent/runtime.py::_tenant_budget_envelope` *(FR-018)*
- [x] T010 [P] The per-node lifecycle logging wrapper `_instrumented` in `app/agent/graph_utils.py` *(FR-006, FR-007)*
- [x] T011 [P] Liveness and readiness probes (five checks, each bounded to 2 s) in `app/api/health.py` *(FR-010)*

**Checkpoint**: Foundation ready — metrics can be created, logs structured, usage recorded, an allowance computed.

---

## Phase 3: User Story 1 — An operator sees how the whole system is behaving from one place (Priority: P1) 🎯 MVP

**Goal**: Every process's metrics and logs in one place, correlated by one id.

**Independent Test**: Run the API and two worker replicas; send turns; one metrics source shows all three and one log search for a turn id spans processes.

### Tests for User Story 1

- [x] T012 [P] [US1] Outcome, retry, approval, capability-gate, degraded-retrieval and compaction counters through the real graph; tool-call and tool-error counters via the callback — in `tests/core/test_metrics.py` (`TestNodeLevelMetrics`, `TestToolCallbackMetrics`) *(FR-003, SC-001)*
- [x] T013 [P] [US1] JSON shape, extras, stringified extras, exceptions, and the correlation id (in scope, outside, reset, nested, explicit wins, reset on error) — in `tests/core/test_logging_config.py` *(FR-006)*
- [x] T014 [P] [US1] Readiness — all healthy, one down, all down, a hung probe bounded — in `tests/api/test_health.py` *(FR-010)*

### Implementation for User Story 1

- [x] T015 [US1] Process wiring — `configure_logging` and `configure_telemetry` in the API's lifespan, the agent worker, the ingest worker and the Telegram channel — in `app/api/main.py`, `app/job_queue/agent_worker.py`, `app/ingestion/ingest_worker.py`, `app/channels/telegram.py` *(FR-001)*
- [x] T016 [P] [US1] The provisioned dashboards and their provisioning — in `observability/grafana/dashboards/agent-overview.json`, `observability/grafana/dashboards/logs.json`, `observability/grafana/provisioning/dashboards/dashboards.yml` *(FR-010)*
- [x] T017 [US1] Per-turn metrics — outcome, latency, iterations, tokens — in `_record_turn_metrics` in `app/agent/runtime_stream.py` *(FR-003)*

### Open follow-ups for User Story 1 (not built) — **A5**

- [ ] T018 [US1] **A5 — write the failing test first**: in `tests/core/test_metrics.py` add a test that `.labels(...)` with a key not in an instrument's declared `labelnames` is rejected (raises under test; counted and dropped in production), plus a guard that walks the AST of `app/` and `scripts/` and checks every `.labels(...)` call against its declaration. Fails today (the declaration is stored and never read) *(FR-005)*
- [ ] T019 [US1] **A5 — fix**: enforce `labelnames` in `Counter.labels` and `Histogram.labels` in `app/core/metrics.py` *(FR-005)*

**Checkpoint**: US1 works as built for the API and workers; T018–T019 are hardening.

---

## Phase 4: User Story 2 — Every turn's usage is recorded against the organization and the model that served it (Priority: P1)

**Goal**: One ledger row per turn; the caller's own totals; the concrete model recorded.

**Independent Test**: Complete a turn; read the row and the summary; repeat for another tenant.

### Tests for User Story 2

- [x] T020 [P] [US2] `GET /usage` is a tenant-scoped pass-through — in `tests/api/test_api.py` (`TestUsage`) *(FR-017)*
- [x] T021 [P] [US2] Alias resolution — known, unknown, cached after success, degrades on connection failure and on an error status, strips the `/v1` suffix — in `tests/agent/test_model_resolver.py` *(FR-013)*
- [x] T022 [P] [US2] The in-run cost ceiling reads the same price table (a patched huge price) — in `tests/agent/test_graph_integration.py` *(FR-014)*

### Implementation for User Story 2

- [x] T023 [US2] `GET /usage` — the caller's tenant totals, all-time and rolling-24-hour, with the limit — in `app/api/main.py` and `app/api/schemas.py` (`UsageResponse`) *(FR-017)*
- [x] T024 [US2] The ledger write on a completed turn, from `_record_turn_metrics` in `app/agent/runtime_stream.py`, and from the two scheduled scripts in `scripts/ops_digest.py`, `scripts/followup_sweep.py` *(FR-013, SC-003)*

### Open follow-ups for User Story 2 (not built) — **B17, B19, A2, A3, A9 (ledger)**

- [ ] T025 [US2] **B17 — write the failing test first**: in `tests/agent/test_streaming_terminal_events.py` add a test that a turn whose model returns a 500-token step and then stalls past a shortened `REQUEST_TIMEOUT_SECONDS` records 500 tokens through `usage_ledger.record_usage` and increments `agent_tokens_total` (and likewise for an erroring and a cancelled turn). Fails today (reproduced — quickstart *Scenario B17*) *(FR-015, SC-004)*
- [ ] T026 [US2] **B17 — fix**: in the timeout, error and cancel branches of `_run_graph_stream` in `app/agent/runtime_stream.py` read the last checkpoint (`aget_state`) and pass its state, ctx and thread to `_record_turn_metrics`; correct the helper's "have total_tokens == 0 anyway" comment; decide in the same change how a paused turn that is never resumed is recorded *(FR-015)*
- [ ] T027 [US2] **B19 — write the failing test first**: in `tests/agent/test_model_resolver.py` add a test that `record_usage` does not block a concurrently running heartbeat when the resolver's HTTP call is slow, and that a failed lookup is not repeated on the next call within the cache interval. Fails today (reproduced — *Scenario B19*: a 1.02 s stall, two attempts) *(FR-016, SC-008)*
- [ ] T028 [US2] **B19 — fix**: make `resolve_model` in `app/agent/model_resolver.py` asynchronous (or hop to a worker thread) and add a short negative cache; update its callers in `app/agent/usage_ledger.py` *(FR-016)*
- [x] T029 [US2] **A2 — decided and built (prices come from LiteLLM `/model/info` via `app/agent/pricing.py`; the ledger stores the node's running `total_cost_usd`; `UNPRICED_MODEL_POLICY` allow|block)**: original task — **A2 — decide, then test first**: in `tests/agent/test_usage_ledger.py` pin the chosen pricing rule — price by the resolved concrete model from configuration rather than a code table keyed by alias; warn once per alias when tokens are recorded with no price; price the in-run ceiling by the node's own model (`record.model` for a delegated run) — then implement in `app/agent/usage_ledger.py` and `app/agent/graph_agent_node.py` *(FR-024)*
- [x] T030 [US2] **(built: `postgres-init/17-usage-ledger-indexes.sql` (numbered 17, 16 was taken) + `scripts/usage_ledger_sweep.py` + `USAGE_LEDGER_RETENTION_DAYS` (floor 35))** **A3 — add the index and a retention policy**: a new numbered `postgres-init/16-usage-ledger-recorded-index.sql` creating `(tenant, recorded_at)` (header says how an existing volume applies it), and a retention job or documented partitioning in `scripts/`; verify with the measurement in quickstart *Measure — A3* *(FR-025)*
- [x] T031 [US2] **(built: `tests/integration/test_ledger_real_postgres.py`)** **A9 — real-database ledger tests**: new `tests/integration/test_ledger_real_postgres.py` (marked `integration`, using `tests/containers.py::ensure_postgres`) covering `record_usage`'s row (parameters, price, the no-op rules) and `usage_summary`'s `WHERE` against the real tables; plus hermetic tests in `tests/agent/test_usage_ledger.py` for the price table and the no-op rules *(FR-027)*

**Checkpoint**: US2 is *verified* only after T025–T028.

---

## Phase 5: User Story 3 — An organization cannot spend past its daily allowance, even with many turns at once (Priority: P1)

**Goal**: A rolling-24-hour ceiling enforced before any work, with a reservation closing the concurrency race.

**Independent Test**: Spend a tiny ceiling; confirm the next turn is refused before the graph is touched; start many at once near the line.

### Tests for User Story 3

- [x] T032 [P] [US3] The allowance check — under, at, over, invalid ctx, fails open, the 80% warning, a rolling window scoped to the tenant, spend + reservations — in `tests/agent/test_tenant_budget.py` (`TestTenantOverDailyBudget`) *(FR-018, SC-005)*
- [x] T033 [P] [US3] Reserve and release forward and round-trip; a refused turn never touches the graph; an under-budget turn does not short-circuit — in `tests/agent/test_tenant_budget.py` (`TestReserveAndReleaseTurnBudget`, `TestEntryPointsRefuseBeforeTouchingTheGraph`) *(FR-018, FR-019, SC-006)*
- [x] T034 [P] [US3] The reservation statements — upsert, floored release, stale excluded at read, fail-open on a write or read error (a fake cursor: statement *shape* only) — in `tests/agent/test_usage_ledger.py` *(FR-019, FR-020)*

### Implementation for User Story 3

- [x] T035 [US3] `_tenant_over_daily_budget`, `_reserve_turn_budget`, `_release_turn_budget` and the 80% warning in `app/agent/runtime.py`; the refusal envelope *(FR-018, FR-019)*
- [x] T036 [US3] The entry-point gate — check, refuse before the graph, reserve, run, detached release in a `finally` — in `astream_events_turn` in `app/agent/runtime_stream.py` *(FR-018, FR-019)*
- [x] T037 [US3] `reserve_budget` (upsert), `release_budget_reservation` (floored), `in_flight_reservation` (5-minute staleness at read) in `app/agent/usage_ledger.py` *(FR-019, FR-020, SC-006)*

### Open follow-ups for User Story 3 (not built) — **B20, A6**

- [ ] T038 [US3] **B20 — write the failing test first**: new `tests/integration/test_reservation_real_postgres.py` (marked `integration`, `tests/containers.py::ensure_postgres`) that reserves 0.50, ages the row 10 minutes, asserts the read is 0, reserves again and asserts the read is 0.50 (not 1.00), and that a completed turn's release returns the read to 0. Fails today (reproduced against a real database — quickstart *Scenario B20*) *(FR-021, SC-007)*
- [ ] T039 [US3] **B20 — fix** in `reserve_budget` in `app/agent/usage_ledger.py`: reset a stale row on reserve (`CASE WHEN tenant_budget_reservations.updated_at < now() - interval '5 minutes' THEN EXCLUDED.reserved_usd ELSE tenant_budget_reservations.reserved_usd + EXCLUDED.reserved_usd END`), or move to one row per turn with its own timestamp and sum the fresh ones (exact; larger). Correct the "self-heals" comment *(FR-021)*
- [x] T040 [US3] **(decided and built: resume re-checks the allowance and holds a reservation; crash-continue holds a reservation but is not refused; cancel is never gated)** **A6 — decide, then test first**: either re-check the allowance (without a new reservation) in `astream_events_resume` and `astream_events_continue_turn` in `app/agent/runtime_stream.py`, with a failing test in `tests/agent/test_tenant_budget.py` that an over-budget tenant's resume is refused — or document that a paused turn is pre-authorized, in `GRAPH_PATTERNS.md` *(FR-022)*

**Checkpoint**: US3 is *verified* only after T038–T039.

---

## Phase 6: User Story 4 — A human is told about conditions they would otherwise never notice (Priority: P2)

**Goal**: Alert rules over defined metrics, delivered to a person, covering the silent failures.

**Independent Test**: Stop a covered dependency and confirm its alert fires; stop the whole worker pool and confirm one fires.

### Tests for User Story 4

*(None built — the alert rules and dashboards have no test and no CI validation: A9.)*

### Implementation for User Story 4

- [x] T041 [US4] The thirteen alert rules, the Prometheus config and the Alertmanager route in `observability/prometheus/alerts.yml`, `observability/prometheus/prometheus.yml`, `observability/alertmanager/alertmanager.yml` *(FR-010, FR-004)*
- [x] T042 [P] [US4] The degrade-and-pivot counters the rules read — `agent_team_channel_notify_total`, `agent_tool_dedup_degraded_total`, `agent_upload_failed_total` and the rest — in `app/core/metrics.py` and their call sites `app/domains/notify.py`, `app/agent/tool_idempotency.py`, `app/api/main.py` *(FR-004)*

### Open follow-ups for User Story 4 (not built) — **B22, B21, A1, A4, A7, A9 (validators)**

- [ ] T043 [US4] **B22 — write the failing test first**: new `tests/scripts/test_scheduled_telemetry.py` asserting that each of `scripts/ops_digest.py`, `scripts/followup_sweep.py`, `scripts/tool_call_dedup_sweep.py` and `scripts/ops_investigate.py` exposes a `main()` that calls `configure_telemetry` (patched) before doing work. Fails today (the startup is an inline `if __name__ == "__main__":` that configures logging only — reproduced, quickstart *Scenario B22*) *(FR-028, SC-011)*
- [ ] T044 [US4] **B22 — fix**: in each of the four scripts (`scripts/ops_digest.py`, `scripts/followup_sweep.py`, `scripts/tool_call_dedup_sweep.py`, `scripts/ops_investigate.py`) move the startup into `main()`, call `configure_telemetry("<script name>")` there and make sure the provider is flushed before the process exits (a short-lived script otherwise loses the last interval) *(FR-028)*
- [ ] T045 [US4] **B21 — write the failing tests first**: in `tests/job_queue/test_queue.py` that the first-event deadline expiry in `read_results` increments a new `agent_worker_unreachable_total{kind}`; in `tests/api/test_health.py` that readiness reports whether a consumer exists for the requests stream; and in a new `tests/observability/test_alerts_and_dashboards.py` that a rule exists on that counter and on an `absent`-style condition for `agent_requests_total`. Fail today (read from code) *(FR-012, SC-009)*
- [ ] T046 [US4] **B21 — fix**: add `agent_worker_unreachable_total` to `app/core/metrics.py` and increment it in `app/job_queue/queue.py::read_results` and `app/ingestion/ingest_queue.py::read_results`; publish a consumer-lag gauge from the group's pending count; add the rules to `observability/prometheus/alerts.yml`; add a consumer probe to `app/api/health.py` *(FR-012)*
- [x] T047 [US4] **(built: `tests/agent/test_cost_governance_degrade_counters.py`)** **A1 — write the failing tests first**: in `tests/agent/test_usage_ledger.py`, `tests/agent/test_tenant_budget.py` and `tests/agent/test_model_resolver.py` assert that a failed ledger write, ledger read, reservation write, release, reservation read and model resolution each increment a counter. Fail today (log lines only) *(FR-023, SC-010)*
- [x] T048 [US4] **(built for the ledger, allowance and resolver paths (`price_lookup` came with the pricing PR); alerts `LedgerWriteFailing`, `TenantAllowanceUnenforced`)** **A1 — fix**: one counter `agent_cost_governance_degraded_total{path}` (`ledger_write`, `ledger_read`, `reservation`, `model_resolve`) in `app/core/metrics.py`, incremented at each site in `app/agent/usage_ledger.py`, `app/agent/runtime.py` and `app/agent/model_resolver.py`; an alert rule on `ledger_write` (a lost ledger row is committed spend a human will not learn about) in `observability/prometheus/alerts.yml` *(FR-004, FR-023)*
- [ ] T049 [US4] **A4 — rules and attribution**: rules in `observability/prometheus/alerts.yml` for reclaimed jobs, circuit-breaker opens, ML-moderation degradation and delegated-run failures; log the tenant at the allowance refusal in `app/agent/runtime.py`; give `agent_subagent_duration_seconds` a bucket View in `app/core/telemetry.py`; count `timeout` in `HighTurnErrorRate` or add a timeout rule *(FR-004)*
- [ ] T050 [US4] **A7 — production receiver**: a notification block for Alertmanager in the production stack (`observability/alertmanager/alertmanager.yml` for local, a production variant mounted by `docker-compose.observability.prod.yml`) fed from the deployment's secrets; a check that the production compose does not mount a receiver-less file *(FR-011)*
- [ ] T051 [US4] **A9 — alert and dashboard validators, test first**: new `tests/observability/test_alerts_and_dashboards.py` that parses `observability/prometheus/alerts.yml` and every `observability/grafana/dashboards/*.json`, and asserts every `agent_*` metric they reference is defined in `app/core/metrics.py` (the cross-check of quickstart *Check — A4 and A5*); and a `promtool check rules` step in `.github/workflows/ci.yml` (`docker`) *(FR-010, FR-027)*

**Checkpoint**: US4 is *verified* only after T043–T046 — until then a failed digest push and a dead worker pool are both invisible.

---

## Phase 7: User Story 5 — Logs and metrics describe what happened without recording what anyone said, and telemetry costs the process almost nothing (Priority: P2)

**Goal**: Content-free logs and metrics; a trace for inspection; no per-turn resource growth.

**Independent Test**: Search every log line and label for a planted secret (none match); run 100 turns and compare the thread count.

### Tests for User Story 5

- [x] T052 [P] [US5] The audit log never carries raw tool args or results, and carries correlated start, success and failure lines — in `tests/core/test_metrics.py` (`TestToolCallAuditLog`) *(FR-007, SC-002)*
- [x] T053 [P] [US5] The trace records the text the client saw — cache hit and normal stream — in `tests/agent/test_streaming_terminal_events.py` (`TestTraceOutputMatchesWhatTheClientActuallySaw`) *(FR-008)*

### Implementation for User Story 5

- [x] T054 [US5] `MetricsCallbackHandler` and `_fingerprint` in `app/core/metrics.py`; the logged-error-class-only rule in `app/agent/runtime_stream.py` (`graph_stream_failed`) *(FR-007)*
- [x] T055 [US5] `_open_trace` and the end-of-turn flush — best-effort, a trace per turn and per resume — in `app/agent/runtime_stream.py`, `app/agent/runtime_legacy_stream.py` *(FR-008)*

### Open follow-ups for User Story 5 (not built) — **B18, A10**

- [ ] T056 [US5] **B18 — write the failing test first**: new `tests/core/test_tracing_client.py` that opening and flushing a trace for 20 turns (the SDK client patched to count constructions and its threads) constructs **one** client, not 40. Fails today (reproduced — quickstart *Scenario B18*: 60 threads after 10 turns) *(FR-009, SC-008)*
- [ ] T057 [US5] **B18 — fix**: a lazily created process-wide tracing client shared by `_open_trace` and the flush in `app/agent/runtime_stream.py` and `app/agent/runtime_legacy_stream.py`, flushed at turn end and shut down at process stop; keep the disabled-when-no-keys behavior *(FR-009)*
- [ ] T058 [US5] **A10 — decide**: either amend Principle V's "logs and traces carry metadata only" through `/speckit-constitution` and a PR (traces are the one access-controlled, content-bearing channel, with a retention and scrubbing rule), or change what the callback records in `app/agent/runtime_stream.py::_open_trace`. Record the decision in `GRAPH_PATTERNS.md` pattern 14 and `contracts/logs-and-traces.md`. Not a code task until decided *(FR-007)*

**Checkpoint**: US5's content rules hold for logs and metrics; its resource rule is verified only after T056–T057.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T059 **A8 — configuration**: add `MAX_COST_USD_PER_TURN`, `MAX_COST_USD_PER_TENANT_PER_DAY` and `REQUEST_TIMEOUT_SECONDS` with a one-line why each to `.env.example`; a guard that every `Settings` field has an entry would prevent a repeat (features 004 and 007 found the same class) *(FR-026)*
- [ ] T060 **A8 — comments and docs**: add `cancelled` to the `agent_requests_total` comment in `app/core/metrics.py`; correct the reservation "self-heals" claim in `app/agent/usage_ledger.py`; update `GRAPH_PATTERNS.md` patterns 11, 14, 26, 35 and 38 and "Extending Further" with B17–B22 and A1–A10 *(FR-026)*
- [ ] T061 After each fix, re-run `quickstart.md`'s scenario for it and delete its row from `plan.md` *Complexity Tracking*

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup** → **Foundational** → **User Stories**. Everything `[x]` already exists.
- **US1, US2, US3** (P1) need only Phase 2; US3's allowance reads US2's ledger. **US4** (P2) reads the metrics of every story; **US5** (P2) is independent.
- **Polish** last — except T059, which can land any time.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T038–T039 (B20) | `usage_ledger.py`, one integration test | test-first against a real database; a safety control with a silent failure mode — **first** |
| 2 | T025–T026 (B17) | `runtime_stream.py`, one test file | test-first; decide the never-resumed pause in the same change |
| 3 | T056–T057 (B18) | `runtime_stream.py`, `runtime_legacy_stream.py`, one new test | test-first; small |
| 4 | T027–T028 (B19) | `model_resolver.py`, `usage_ledger.py`, one test file | test-first; small |
| 5 | T043–T044 (B22) | four scripts, one new test | test-first; small; unblocks the existing alert |
| 6 | T045–T046 (B21) | `metrics.py`, both `read_results`, `health.py`, `alerts.yml`, tests | new signal and rule; larger — split the readiness probe out if over budget |
| 7 | T047–T049, T051 (A1, A4, A9 validators) | counters, rules, a validator test, CI | after PR 6 so the rule file is edited once |
| 8 | T029–T031 (A2, A3, A9 ledger) | pricing, one SQL file, a retention job, integration tests | needs the pricing decision |
| 9 | T018–T019 (A5), T040 (A6) | `metrics.py`, `runtime_stream.py`, tests | small, independent |
| 10 | T050 (A7), T058 (A10), T059–T061 (A8) | deployment config, constitution PR, docs | decisions and docs; can land any time |

PRs 1–5 are mutually independent; run them in parallel.

### Parallel opportunities

- Setup T001–T006 and Foundational T007–T011 are [P].
- After Phase 2, US1/US2/US3 in parallel; within a story every test task is [P].

## Parallel Example: User Story 3

```bash
# Tests together (different classes in one file, different files):
Task: "T032 Allowance check in tests/agent/test_tenant_budget.py"
Task: "T034 Reservation statements in tests/agent/test_usage_ledger.py"
# Implementation together:
Task: "T035 Allowance and reservation helpers in app/agent/runtime.py"
Task: "T037 Reservation statements in app/agent/usage_ledger.py"
```

## Implementation Strategy

### As-built order (what happened)

The usage ledger, the resolved-model column and the per-turn cost ceiling landed together on 2026-08-22. The per-tenant daily allowance arrived on 08-27 with the structured logging, real health checks and rate limiting;
on 08-29 metrics moved to OpenTelemetry — pushed, not pulled — together with structlog and the Grafana/Loki/Prometheus stack. A high-concurrency audit on 09-22 found that N simultaneous turns could all pass the same
stale spend figure, which added the reservation table and the detached release. The degrade-path alert rules arrived on 10-01 after a round of "degrade, don't fail" changes. The six defects sit at *seams the happy
path never crosses*: a turn that does not finish, a process that is not a service, a tracing client built per call, a synchronous call in async code, a crash between reserve and release, and the absence of anything
that says "nobody is answering".

### Closing the open follow-ups (what to do next)

1. **PR 1 (B20)** now — a safety control that fails silently and permanently, with a one-statement fix.
2. **PRs 2–5 (B17, B18, B19, B22)** — independent; the first and the last close visibility and accounting gaps cheaply.
3. **PR 6 (B21)** — the signal for "nothing is answering".
4. **PRs 7–10** — counters and rules for the quiet failures, pricing and the index, then the decisions and docs.
5. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 + US3 (T001–T037) is the minimum that measures the system, records spend and enforces an allowance. None of B17–B22 weakens tenant isolation, so the feature is *safe* as it stands — but not **fully
correct**: B17 loses the spend of the turns most likely to run long, B20 can wedge a tenant after enough crashes, B18 and B19 cost every worker threads and stalls, and B21 and B22 leave two kinds of outage invisible.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (81 passed) was re-run on 2026-10-03, plus the reproduction scenarios, the cross-check scripts and the two throwaway-database measurements.
- Tier 2 (there is none for this feature), the live tier and the monitoring stack itself were **not** run by this batch.
- Features 001 (the per-turn ceilings and the output checks), 003 and 004 (the queue, workers, crash recovery and the first-event deadline), 007 (the delegated-run metrics and ledger rows) and 009 (the notifier's
  sandbox and crawler callers) own behavior this feature relies on.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks; do not point the B20 or A3 experiments at the project's own database.
