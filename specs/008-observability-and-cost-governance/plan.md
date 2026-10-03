# Implementation Plan: Observability and Cost Governance

**Branch**: `008-observability-and-cost-governance` | **Date**: 2026-10-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/008-observability-and-cost-governance/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

Three cooperating layers.

**Telemetry out.** `app/core/metrics.py` wraps the OpenTelemetry API in a small prometheus_client-shaped surface (`Counter.labels(...).inc()`, `Histogram.observe()`), so 53 instruments are created at
import against a proxy meter. `app/core/telemetry.py::configure_telemetry(service_name)` — called once at real process start (the API's lifespan, or a worker or channel `__main__`), never at import — installs a
`MeterProvider` that **pushes** over OTLP/HTTP every 15 s to an `otel-collector`, whose single Prometheus exporter (`:8889`) is the only scrape target for the whole application, including every worker replica
(which bind no port). Two histograms get explicit bucket Views. Prometheus (15 d retention) evaluates `observability/prometheus/alerts.yml` (13 rules) and sends to Alertmanager (a receiver with no delivery
settings). Grafana provisions six dashboards. Logs go through `app/core/logging_config.py` (structlog over stdlib `logging`): one JSON object per line, a contextvar correlation id, shipped by Promtail to Loki
(7 d). `MetricsCallbackHandler` counts tool calls and errors and logs a per-call audit line carrying only a SHA-256 fingerprint. Langfuse tracing is optional and opened per turn in `runtime_stream.py::_open_trace`.

**Usage in.** `app/agent/usage_ledger.py::record_usage` inserts one `usage_ledger` row per completed turn (`tenant, principal, thread_id, model_alias, total_tokens, cost_usd, resolved_model`), costing tokens ÷ 1000 × an
alias-keyed price table (`$0` otherwise) and resolving the concrete model best-effort through the proxy's admin endpoint (`app/agent/model_resolver.py`). `usage_summary` sums by tenant (and optionally principal, and
a `since` timestamp); `GET /usage` exposes the caller's tenant totals.

**Allowance enforced.** `runtime.py::_tenant_over_daily_budget` (called once, from `astream_events_turn`) sums the trailing 24 hours plus the in-flight reservation and refuses at or above
`MAX_COST_USD_PER_TENANT_PER_DAY` ($20), warning at 80%. A proceeding turn reserves `MAX_COST_USD_PER_TURN` ($0.50) in `tenant_budget_reservations` (an upsert) and releases it in a `finally` through a detached task.
Every ledger and reservation failure fails open.

The plan records honestly that six defects — **B17** (a turn that does not complete records no usage), **B18** (a tracing client, and its three threads, per turn plus one per flush), **B19** (usage recording blocks
the event loop), **B20** (a leaked reservation is resurrected, not healed) **B21** (a worker-pool outage is invisible) and **B22** (scheduled jobs never export metrics) — and ten smaller gaps sit around a design whose *isolation and fail-open posture* hold
where they are asserted.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `opentelemetry-sdk` 1.44.0 and `opentelemetry-exporter-otlp-proto-http` 1.44.0 (metrics), `structlog` 25.5.0 (over stdlib `logging`), `langfuse` 2.60.10 (optional tracing), `psycopg` 3.3.4 via the
pooled appdata connections in `app/agent/sql_store.py` (ledger and reservations), `httpx` 0.28.1 (the model resolver, the readiness probes). Stack images (all `:latest`): OpenTelemetry Collector contrib,
Prometheus, Alertmanager, Loki, Promtail, Grafana (`docker-compose.observability.yml`, `docker-compose.observability.prod.yml`).

**Storage**: Postgres `appdata` — `usage_ledger` (`postgres-init/03-meter.sql`, `04-resolved-model.sql`; index `(tenant, principal)` only) and `tenant_budget_reservations` (`12-tenant-budget-reservations.sql`; one row per
tenant, primary key `tenant`); Prometheus TSDB (15 d); Loki (168 h); Langfuse's own store (optional, separate).

**Testing**: pytest hermetic tier — `tests/core/test_metrics.py` (counters through the real graph, the tool callback and its audit lines), `tests/core/test_logging_config.py`, `tests/agent/test_usage_ledger.py`
(reservation statements against a fake cursor), `tests/agent/test_tenant_budget.py` (the allowance check, reserve/release, the entry point's short circuit), `tests/agent/test_model_resolver.py`,
`tests/api/test_health.py`, `tests/api/test_api.py::TestUsage`, `tests/agent/test_streaming_terminal_events.py` (trace output). **Not tested**: `record_usage`'s statement and no-op rules, `usage_summary`'s `WHERE`, the price
table, any statement against a real database, `configure_telemetry`, the alert rules, the dashboards, or any telemetry resource behavior (A9, B18, B19, B20).

**Target Platform**: Linux containers and host-native processes; the observability stack is separate and optional (`make obs-up`).

**Project Type**: Library modules + a metrics wrapper + config for a six-component monitoring stack.

**Performance Goals**: None asserted. Measured: the rolling-window read on a 600,000-row ledger — 9.3 ms (200,000 rows scanned for one tenant) versus 1.5 ms with a `(tenant, recorded_at)` index (A3).

**Constraints**: OTLP export every 15 s; latency buckets `0.5, 1, 2.5, 5, 10, 15, 30, 60, 90, 120` s and step buckets `1, 2, 3, 4, 5, 7, 10, 15`; allowance `MAX_COST_USD_PER_TENANT_PER_DAY = 20.0`, per-turn
ceiling/reservation `MAX_COST_USD_PER_TURN = 0.50`, warning fraction 0.8, reservation stale after 5 minutes, resolver timeout 5 s, request timeout 60 s.

**Scale/Scope**: 53 instruments (21 with a label, all closed sets), 13 alert rules, 6 dashboards, 2 tables, 1 HTTP endpoint (`GET /usage`) plus the two health endpoints.

**Unknowns**: none — every value is read from the repository, measured, or reproduced.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | Yes | **PASS for isolation; B17/B20 are *accounting* defects** | Every ledger row carries `tenant` and `principal`; `usage_summary` always filters by tenant; `GET /usage` takes the tenant from the identity, never a parameter; reservations are per tenant; metric labels carry no tenant. `record_usage` and the allowance no-op/pass without a valid identity. **B17**: unfinished turns never reach the ledger; **B20**: a tenant's reservation can be inflated by crashed turns and wedge that tenant only. |
| II | Mandatory human approval (NN) | No | n/a | Telemetry and accounting are not agent tools. |
| III | Fixed, typed tools | No | n/a | — |
| IV | Exactly-once side effects (NN) | Marginal | **n/a** | The ledger row is an accounting record of real spend, not a business side effect: a turn that genuinely runs twice should record twice. It is not keyed for dedup, deliberately. |
| V | Bounded, observable failure | **Primary** | **FAIL on 6 defects and 3 gaps (B17–B22, A1, A4, A7); the rest PASS** | Bounded: every wait has a timeout (resolver 5 s, readiness probes 2 s, request timeout), the reservation read ignores stale rows, the release clamps at zero. Fail-open is deliberate and documented for every defense-in-depth read. Not met: **B21** (a worker-pool outage emits nothing), **A1** (the ledger, allowance and resolver degrade paths are log-only — Principle V requires a counter), **A7** (no alert reaches a person), **B18/B19** (telemetry costs the process threads and blocking). The "logs and traces carry metadata only" bullet is **not what the system does for traces** — see A10 and the next row. |
| VI | Untrusted content is data | Yes | **PASS with A10** | Logs carry metadata only and an exception's class, never its text (`tests/core/test_metrics.py::TestToolCallAuditLog::test_audit_log_never_carries_raw_tool_args_or_result`; the stream core's `graph_stream_failed` line carries the class only — read, not asserted by a test); tool results are credential-scrubbed before reaching a trace; the full exception text goes only to the optional trace. |
| VII | Test discipline | Yes | **PASS with the known gap (A9)** | The allowance logic and its entry point are well tested against fakes. The ledger's own statements and the reservation SQL are not run against a real database; no test validates the alert rules or dashboards. |
| VIII | Why-first docs, honest gaps | Yes | **PASS with drift (A8)** | Module docstrings and pattern text carry the reasoning (push versus pull, bucket choice, the reservation race). Drift: three tunables missing from the example environment, a stale outcome list, and the reservation comment's "self-heals" claim, which B20 disproves. |
| — | Constitution Principle V, last bullet | **Primary** | **Conflict (A10)** | "Logs and traces carry metadata only … never message content." Traces carry content by design. Needs a decision through `/speckit-constitution`, not an edit here. |
| — | *Configuration* constraint | Yes | **FAIL on A8** | "Tunables live in `Settings` with a matching `.env.example` entry." `MAX_COST_USD_PER_TURN`, `MAX_COST_USD_PER_TENANT_PER_DAY` and `REQUEST_TIMEOUT_SECONDS` have none. |

**Gate result (pre-research)**: no violation of a NON-NEGOTIABLE principle — tenant isolation holds everywhere it is asserted and the allowance is enforced before any model work. **B17–B22 are Principle V /
accounting defects**: five were reproduced (B17 against the real graph, B18 against the installed SDK, B19 and B20 against the real function and a real database, B22 against a script's real startup path) and one established by reading (B21). A10 is a conflict
between the constitution and the code that only the constitution process can resolve. They are *defects*, not justified exceptions; the plan proceeds because it describes shipped code.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged. Writing the metric catalog made the reachability gap (A4) and the missing worker-outage signal (B21) obvious as *absences* —
there is no row in the table for "no turns are being processed" — and writing the reservation lifecycle in `data-model.md` §3 is what exposed B20: the staleness rule lives on the *read*, the accumulation on the *write*,
and nothing connects them.

## Project Structure

### Documentation (this feature)

```text
specs/008-observability-and-cost-governance/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the incidents behind each; B17–B22 and A1–A10 as findings
├── data-model.md                  # Phase 1 — the metric catalog, the ledger, the reservation lifecycle, log shape, settings
├── quickstart.md                  # Phase 1 — runnable checks per tier, incl. the B17–B20 and B22 reproductions
├── contracts/
│   ├── metrics-and-alerts.md      # push topology, instruments, buckets, alert rules, what is and is not watched
│   ├── usage-and-allowance.md     # the ledger row, GET /usage, the allowance check, reserve/release
│   └── logs-and-traces.md         # the log line, the correlation id, the audit line, what traces hold
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/core/
├── metrics.py                     # Counter/Histogram wrapper, 53 instruments, MetricsCallbackHandler, _fingerprint
├── telemetry.py                   # configure_telemetry: OTLP push, Views for explicit buckets
├── logging_config.py              # structlog over stdlib logging; bind_request_id; JSON formatter
└── config.py                      # OTEL_EXPORTER_OTLP_ENDPOINT, MAX_COST_*, REQUEST_TIMEOUT_SECONDS, LANGFUSE via env
app/agent/
├── usage_ledger.py                # record_usage, usage_summary, reserve_budget, release_budget_reservation, in_flight_reservation, PRICE_PER_1K_TOKENS_USD
├── model_resolver.py              # resolve_model (sync httpx, cached on success only)
├── runtime.py                     # _tenant_over_daily_budget, _tenant_budget_envelope, _reserve_turn_budget, _release_turn_budget
├── runtime_stream.py              # _record_turn_metrics, _open_trace, astream_events_turn (check, reserve, detached release)
└── graph_agent_node.py            # the in-run cost bookkeeping (priced by the global default alias)
app/api/main.py · app/api/health.py            # GET /usage; /health and /health/ready
observability/
├── prometheus/ (prometheus.yml, prometheus.prod.yml, alerts.yml)
├── otel-collector/ (config.yaml, config.agent.yaml) · alertmanager/alertmanager.yml · loki/ · promtail/
└── grafana/ (provisioning/, dashboards/*.json)
docker-compose.observability.yml · docker-compose.observability.prod.yml
postgres-init/03-meter.sql · 04-resolved-model.sql · 12-tenant-budget-reservations.sql
tests/core/ · tests/agent/test_usage_ledger.py · test_tenant_budget.py · test_model_resolver.py · tests/api/test_health.py
```

**Structure Decision**: Metrics are one wrapper module so call sites never import OpenTelemetry; the ledger and the reservation share one module because both are "this application's own operational data" in the same
database; the allowance check lives in the runtime, next to the turn it gates, and reads the ledger only through `usage_ledger`. Fail-open is chosen per read, never globally.

## Complexity Tracking

> Filled because the Constitution Check found six defects and several gaps. Defects are listed without a justification column: they are simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B17 (defect, open)** — a timed-out, errored or cancelled turn records no tokens. Reproduced: 500 tokens in the checkpoint, 0 ledger rows, 0 counter. | Not needed — the helper was written for the completed branch and the other branches pass it no state; its comment assumes those turns spent nothing. | In each non-completed branch read the last checkpoint (`aget_state`) and record its tokens before emitting the terminal event; a paused turn that is never resumed needs a decision (record at pause, or on a sweep). |
| **B18 (defect, open)** — a new tracing client per turn plus one per flush, each with three threads that never exit — even with no keys. Reproduced: 60 threads after 10 turns. | Not needed — the SDK's own guidance is one client per process; the opener and the flush each construct their own. | A lazily created process-wide client reused by both, flushed at turn end and shut down at process stop. |
| **B19 (defect, open)** — the model resolver blocks the event loop and never caches a failure. Reproduced: a 1.0 s stall for two recordings. | Not needed — it began as a synchronous helper and was called from async code unchanged. | An async HTTP client (or a worker-thread hop) plus a short negative cache so a failing proxy is asked once per interval, not once per turn. |
| **B20 (defect, open)** — a leaked reservation is resurrected by the next upsert. Reproduced against a real database. | Not needed — staleness was added on the read; the write kept accumulating. | Make the upsert reset the row when it is stale (`CASE WHEN tenant_budget_reservations.updated_at < now() - interval '5 minutes' THEN EXCLUDED.reserved_usd ELSE tenant_budget_reservations.reserved_usd + EXCLUDED.reserved_usd END`), or store one row per turn with its own timestamp and sum the fresh ones. The second is exact; the first is a one-line change. |
| **B21 (defect, open)** — a total worker outage is invisible. | Not needed — the first-event deadline was added to stop an SSE hang, not to emit a signal. | Count the deadline expiry (`agent_worker_unreachable_total{kind}`), publish a consumer-lag gauge from the group's pending count, add rules on both and an `absent`-style rule on `agent_requests_total`, and make readiness report whether a consumer exists. |
| **B22 (defect, open)** — the four scheduled scripts never call `configure_telemetry`; their counters, including every failed team-channel push, go to a no-op provider. Reproduced. | Not needed — each script was written to print for a person and configures logging only; the metrics layer arrived later. | Call `configure_telemetry(<script name>)` in each script's `__main__`, flush on exit, and confirm the digest's counter reaches the collector; or push a heartbeat-style counter from the sweep itself. |
| **A1** — degrade paths are log-only. | Written before the "every degrade path has a counter" rule. | One counter with a `path` label (`ledger_write`, `ledger_read`, `reservation`, `model_resolve`) and an alert on `ledger_write`. |
| **A2** — alias-keyed code price table; wrong alias in one case; no unpriced warning. | A demo default for local models. | Price by the resolved concrete model (already recorded), from configuration; warn once per alias when tokens are recorded with no price; price the in-run ceiling by the node's own model. |
| **A3** — a whole-history scan per turn; no retention. | The index predates the rolling window. | `(tenant, recorded_at)` index in a new numbered SQL file (existing volumes apply it by hand) and a retention job or partitioning. |
| **A4** — 21 metrics unwatched; the refusal names no tenant. | Counters were added with their code. | Rules for reclaims, circuit-breaker opens, ML-moderation degradation and subagent failures; log the tenant at the refusal. |
| **A5** — label names unenforced. | Wrapper kept call sites unchanged. | Validate keys against `labelnames` in `.labels()` (raise in tests, drop and count in production) and a guard test. |
| **A6** — resume and crash-continue skip the allowance. | The check was placed at the one new-turn entry point. | Call the check (without a new reservation) in the resume and continue entry points, or document that a paused turn is pre-authorized. |
| **A7** — no alert is delivered. | A demo default, honestly commented. | A production receiver block fed from the deployment's secrets; a startup check that the production compose does not mount a receiver-less config. |
| **A8** — three settings missing from the example environment; stale comments. | Written feature by feature. | Add the entries, correct the comments and the reservation "self-heals" claim. |
| **A9** — ledger statements and alert/dashboard files untested. | The reservation was developed against fakes; no validator was wired. | Integration tests of the ledger and reservation statements (the container helper exists), a hermetic test that every alert and dashboard metric exists, `promtool check rules` in CI. |
| **A10** — the constitution and the traces disagree. | Langfuse is the sanctioned content channel (pattern 14); the principle was written more broadly. | Amend Principle V through the constitution process (with a retention and scrubbing rule for traces), or strip content from traces. A decision, not a code change. |
