# Research: Observability and Cost Governance

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-03

**Status**: Retrospective — decisions reconstructed from the code, its comments, `postgres-init/` headers, the observability configuration and `GRAPH_PATTERNS.md` patterns 11, 14, 26, 35, 37 and 38. Each entry
names its evidence. **No `NEEDS CLARIFICATION` remains.** R19–R28 (Part C, with R23b) are *findings* from verifying the as-built system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**. *Alternatives are those the code or its docs name or argue against; where none is recorded the entry says so rather than inventing one.*

---

## Part A — Telemetry

### R1. Metrics are pushed over OTLP to one collector, not pulled from each process

- **Decision**: Every long-running process calls `configure_telemetry(service_name)` once at start and pushes every 15 s to an `otel-collector`, whose single Prometheus exporter is the only scrape target.
- **Rationale**: The earlier design — a `/metrics` endpoint on the API — could never see a worker: workers bind no port, so nothing scrapes them. With a push, one target covers the API and every independently scaled replica.
- **Alternatives considered**: a per-process scrape endpoint (rejected: workers have no port; each replica would need service discovery).
- **Evidence**: `app/core/telemetry.py` and `observability/otel-collector/config.yaml` docstrings; `GRAPH_PATTERNS.md` pattern 11. (Introduced 2026-08-29, with the structlog and stack change.)

### R2. Configure at process start, never at import

- **Decision**: `configure_telemetry` runs in the API's `lifespan` or a worker or channel `__main__`; instruments are created at import against a *proxy* meter that replays them once a provider is installed. A blank endpoint skips setup.
- **Rationale**: OpenTelemetry's `set_meter_provider` is call-once; configuring at import let a real network exporter win a race against a test's own provider. A blank endpoint is for a subprocess with no collector nearby.
- **Evidence**: `telemetry.py` docstring; `tests/api/test_api.py` (lifespan not entered under pytest).

### R3. A prometheus_client-shaped wrapper over the OpenTelemetry API

- **Decision**: `Counter` and `Histogram` in `app/core/metrics.py` expose `.labels(**kw).inc()` / `.observe()` and forward to OTel instruments.
- **Rationale**: Existing call sites needed no rewrite when metrics moved to OpenTelemetry; bucket boundaries cannot be set per call in OTel, so they live in Views on the provider.
- **Consequence recorded as A5**: the wrapper stores `labelnames` and never validates against it.
- **Evidence**: `metrics.py` module docstring.

### R4. Explicit histogram buckets sized for this application

- **Decision**: Latency `0.5 … 120` s and iterations `1 … 15` via `View`s matched by instrument name.
- **Rationale**: The SDK defaults, tuned for millisecond web requests, put every real turn (1–90 s) in a single bucket — verified, not assumed.
- **Evidence**: `telemetry.py` comments; the collector config's note that explicit boundaries survive translation unchanged (checked with a throwaway collector).

### R5. Closed label sets, no tenant or content in any label

- **Decision**: 21 of the 53 instruments carry labels, all drawn from small closed sets (`outcome`, `tool`, `reason`, `stage`, `sink`, `dependency`, `queue`, `subagent`, `capability`, `decision`, `source`).
- **Rationale**: Cardinality control, and no tenant or message text in a time-series store. Per-tenant questions are answered from the ledger instead.
- **Consequence recorded as A4**: the budget-exceeded counter cannot say which tenant.
- **Evidence**: `metrics.py` declarations; an AST check of the 53 `.labels(...)` call sites (0 mismatches, 2026-10-03).

### R6. Structured JSON logs with a correlation id carried by a context variable

- **Decision**: structlog's `ProcessorFormatter` on the stdlib root handler; `bind_request_id(id)` wraps one turn or job and a processor stamps the id on every line, including from code that never heard of it; `request_id` is not overwritten if a call supplies one.
- **Rationale**: Plain `logging.basicConfig` dropped every `extra` field; the API had no handler at all under `make serve`; the id used to be attached only at a failure boundary.
- **Evidence**: `app/core/logging_config.py` docstring; `tests/core/test_logging_config.py::TestBindRequestId`.

### R7. Logs and metrics carry metadata only; tool args and results appear as a fingerprint

- **Decision**: `MetricsCallbackHandler` logs the tool name, `run_id` and a 16-hex SHA-256 fingerprint of the arguments and result; error lines carry the exception class; no log line carries message text or the state dict.
- **Rationale**: So the log pipeline cannot become "a second, unscrubbed copy of prompt/document text outside Langfuse" (pattern 14). A fingerprint still answers "was this the same result as last time".
- **Evidence**: `metrics.py::_fingerprint`; `tests/core/test_metrics.py::TestToolCallAuditLog` (`…never_carries_raw_tool_args_or_result`).

### R8. Tracing is optional, per turn, with a trace per resume

- **Decision**: `_open_trace` opens a Langfuse trace and a `CallbackHandler` scoped to it, swallowing any failure; a resume opens its own trace rather than continuing one across a pause.
- **Rationale**: Langfuse answers "what happened in this run"; metrics answer "how often across everyone". Keys are optional, so a missing key must not fail a turn.
- **Consequence recorded as B18 and A10**: the client is built per turn (and again per flush), and traces hold content, which the constitution's wording does not allow.
- **Evidence**: `runtime_stream.py::_open_trace`, `runtime_legacy_stream.py`; the `stateful_client` comment (a `TypeError` once silently swallowed there left every node span unreported).

### R9. The monitoring stack is separate and optional

- **Decision**: `docker-compose.observability.yml` (`make obs-up`) is independent of `make up`; every cross-stack target degrades to harmlessly "down"; nothing in the application depends on it.
- **Rationale**: The application must run without it; the stack is a deployable reference, not a prerequisite.
- **Evidence**: `observability/prometheus/prometheus.yml` header; `README.md` "Observability".

### R10. Alert thresholds are illustrative; Alertmanager has no real receiver by default

- **Decision**: 13 rules with demo thresholds (error rate > 5% for 5 m critical, p95 > 30 s for 10 m, tool errors > 10%, and `increase(...) > 0` for the degrade counters); a receiver with no delivery settings.
- **Rationale**: Guessing at a webhook or SMTP relay the operator has not provided would be "a receiver that silently fails"; alerts still appear in Prometheus and Grafana.
- **Consequence recorded as A7**: the **production** observability compose file mounts the same file.
- **Evidence**: the comment atop `alertmanager.yml`; `alerts.yml` header; `README.md`. The degrade-path rules were added 2026-10-01 ("alert on the pivot-transaction and degrade-don't-fail paths").

---

## Part B — The ledger and the allowance

### R11. A real ledger, written once at the call site where a turn completes

- **Decision**: `record_usage(ctx, thread_id, alias, total_tokens)` inserts one tenant-and-principal-scoped row into `usage_ledger`, called from `_record_turn_metrics` on the *completed* branch only.
- **Rationale**: "No hollow Meter": a shipped default should keep a real ledger rather than a no-op that makes a broken deployment look configured; `MAX_TOKENS_PER_TURN` bounds spend but records nothing durable.
- **Consequence recorded as B17**: "completed" excludes every turn that spent tokens and then timed out, failed or was cancelled.
- **Evidence**: `usage_ledger.py` docstring; pattern 26.

### R12. Cost from an explicit, alias-keyed table; unlisted aliases are free

- **Decision**: `PRICE_PER_1K_TOKENS_USD = {"gpt-4o": 0.005, "gpt-4o-mini": 0.00015}`; cost = tokens ÷ 1000 × price; tokens recorded unconditionally.
- **Rationale**: True for every model this demo runs locally; keeping tokens regardless means pointing at a paid provider is the only change for real cost tracking.
- **Consequence recorded as A2**: aliases are provider-agnostic by design, the table is code, there is no unpriced warning, and the in-run ceiling prices by the global default alias.
- **Evidence**: `usage_ledger.py`; `graph_agent_node.py` (same table, applied incrementally); `tests/agent/test_graph_integration.py` (a patched huge price proves the ceiling path).

### R13. Record the concrete model behind the alias, best-effort

- **Decision**: `resolve_model(alias)` asks the proxy's admin endpoint once per process per alias and caches successes; the ledger stores it in `resolved_model`; never a metric label, never visible to a node.
- **Rationale**: Aliases keep the app portable but let the biggest lever on quality (a gateway remap) change with no recorded artifact; this keeps model choice invisible to routing but visible to forensics.
- **Consequence recorded as B19**: the lookup is synchronous HTTP inside an async function, and failures are not cached.
- **Evidence**: `model_resolver.py` docstring; pattern 38; `tests/agent/test_model_resolver.py`.

### R14. The window is the trailing 24 hours, not a calendar day

- **Decision**: `_tenant_over_daily_budget` sums `usage_ledger` over `now() − 24 h`.
- **Rationale**: A tenant's near-limit state never resets mid-day.
- **Evidence**: `usage_summary` and `_tenant_over_daily_budget` docstrings; `tests/agent/test_tenant_budget.py::TestTenantOverDailyBudget::test_queries_a_rolling_24h_window_scoped_to_this_tenant`.

### R15. A reservation closes the check-then-act race

- **Decision**: After the check passes, `reserve_budget` adds `MAX_COST_USD_PER_TURN` to the tenant's row (an upsert); `_tenant_over_daily_budget` adds the in-flight total to the ledger sum; a `finally` releases it.
- **Rationale**: The ledger only gets a turn's cost *after* it completes, so N concurrent turns read the same stale spend and all passed — found in a high-concurrency audit (2026-09-22).
- **Consequence recorded as B20**: the write accumulates; the staleness rule lives only on the read.
- **Evidence**: `postgres-init/12-tenant-budget-reservations.sql` header; `runtime.py` docstrings; `tests/agent/test_usage_ledger.py`, `test_tenant_budget.py::TestReserveAndReleaseTurnBudget`.

### R16. The reservation is the per-turn ceiling, not an estimate

- **Decision**: Always `MAX_COST_USD_PER_TURN` ($0.50), the hard cap the graph already enforces per turn.
- **Rationale**: It is a safe upper bound by construction. **Note**: for an unpriced (free) model the reserved figure is notional.

### R17. A stale reservation is ignored on read

- **Decision**: `in_flight_reservation` filters `updated_at > now() − 5 min`.
- **Rationale**: A worker that died without releasing must not inflate a tenant's apparent spend forever; "self-heals without needing a background cleanup job".
- **Consequence recorded as B20**: the claim holds only for a row that stays idle; the next write resurrects the amount.
- **Evidence**: `RESERVATION_STALE_AFTER_MINUTES` comment; `…test_excludes_a_stale_reservation_at_the_query_level` (asserts the *read* only).

### R18. Every read and write here fails open

- **Decision**: A failing ledger read, ledger write, reservation write or reservation read logs a warning and lets the turn proceed.
- **Rationale**: These are defense-in-depth layers (Principle IV's dedup-store rule applied to accounting); an outage in the ledger must not also take down every turn. The release runs as a **detached task** because awaiting a database round trip there let a caller react to a terminal event while the thread lock was still held (reproduced by a real-subprocess HITL test).
- **Consequence recorded as A1**: none of these degrades increments a counter, and a failed budget *read* means the allowance is unenforced for that turn.
- **Evidence**: `usage_ledger.py`, `runtime.py`, `runtime_stream.py::astream_events_turn`'s comment.

---

## Part C — Findings (not decisions)

### R19. FINDING B17 — a turn that does not complete records no usage

- **Observation**: `_record_turn_metrics(elapsed, outcome, state, ctx, thread_id)` writes tokens and the ledger row only `if state is not None`; the timeout, error and cancel branches call it with no state; its comment says those branches "have total_tokens == 0 anyway".
- **Reproduction** (temporary harness under `tests/agent/`, deleted after): the real graph with an in-memory checkpoint store and a model whose first step returned a tool call with `usage_metadata.total_tokens = 500`, then slept past `REQUEST_TIMEOUT_SECONDS` (patched to 1 s); the ledger function patched with a recorder; `_run_graph_stream` driven to completion.
  **Observed 2026-10-03**: terminal event `error`/`timeout`; `aget_state(...).values["total_tokens"] == 500`; recorder calls `[]`; `agent_tokens_total` delta `0`.
- **Consequence**: the cross-turn allowance and the ledger under-count exactly the turns that run long and fail. A paused turn that is never resumed is likewise never recorded.

### R20. FINDING B18 — a tracing client and three threads per turn, plus one per flush

- **Observation**: `_open_trace` (and the legacy variant) run `lf = Langfuse()` per turn; the `finally` of the stream core runs `Langfuse().flush()` — a *new* client — per turn.
- **Reproduction** (a standalone script; dummy keys; host `127.0.0.1:9`, so nothing leaves the machine): 20 clients each with one `trace(...)` call → thread count 1 → **61**; 5 more `Langfuse().flush()` → **+15**; 10 simulated turns (two clients each, no references kept, `gc.collect()`, 1.5 s idle) → **61 threads, 60 of them daemon threads still alive**. Three threads per client, whether or not the client is referenced.
- **Also with no keys (the default `.env.example`)**: with `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` unset, `Langfuse()` builds a *disabled* client (`enabled` is `False`), `.trace(...)` still returns a trace object, and the thread count went **1 → 4** for one client. So the leak is in the default configuration, not only where tracing is wanted.
- **Consequence**: about six threads per turn for the life of the process, traced or not.
- **Not verified**: behavior against a *reachable* tracing server (the threads are created at construction, so expected to be the same).

### R21. FINDING B19 — usage recording blocks the event loop

- **Observation**: `record_usage` (async) calls `resolve_model`, which does `httpx.get(..., timeout=5)` synchronously and caches only a successful lookup.
- **Reproduction** (same temporary harness): `httpx.get` replaced by a function that sleeps 0.5 s and raises `ConnectError`; the cache cleared; two `await record_usage(...)` calls with a 10 ms asyncio heartbeat running.
  **Observed 2026-10-03**: **2** resolve attempts (no negative cache); the two calls took **1.01 s**; the heartbeat's longest gap was **1.02 s**.
- **Consequence**: with the real 5 s timeout, each completed turn would freeze every other task on that worker (the ingest worker and agent worker run up to 10 turns at once). The rejected-key case (a non-admin key getting a 401 on the admin endpoint) is the likely real trigger and was **not run**.

### R22. FINDING B20 — a leaked reservation is resurrected

- **Observation**: `reserve_budget`'s statement is `INSERT … ON CONFLICT (tenant) DO UPDATE SET reserved_usd = tenant_budget_reservations.reserved_usd + EXCLUDED.reserved_usd, updated_at = now()`; `in_flight_reservation` reads only rows with `updated_at > now() − 5 min`; the release clamps at zero.
- **Reproduction** (an ephemeral `postgres:16-alpine` container, removed afterwards; the three statements copied from `usage_ledger.py`): (1) reserve 0.50, never release; (2) back-date the row by 10 minutes — the read returns **0 rows**; (3) reserve 0.50 for the tenant's next turn — the read returns **1.000000**; (4) that turn releases its own 0.50 — the read returns **0.500000** with nothing running.
  A loop adding 0.50 thirty-nine more times (a stand-in for 39 more crashes, not a measurement of them) reaches **20.000000**, the default ceiling.
- **Consequence**: each unreleased reservation (a SIGKILL, an out-of-memory kill, a deploy that stops a worker mid-turn, or a lost detached release task) is permanent. After enough of them a tenant is admitted about one turn per idle 5 minutes while spending nothing.

### R23. FINDING B21 — a total worker outage emits nothing

- **Observation**: `read_results(..., first_event_deadline_seconds=...)` yields an `error` event when nothing arrives; no counter is touched. `agent_requests_total` is incremented where a turn runs. The metrics set has no queue-depth, consumer-lag or heartbeat instrument. `alerts.yml` has no `absent`-style rule, and `ScrapeTargetDown` watches `up` for scraped jobs only (the otel-collector stays up when workers stop). `check_dependencies` probes the two databases, the vector store, the queue/cache store and the ML service.
- **Consequence**: every user sees "No response … is an agent-worker running for this domain?" after the deadline while dashboards stay flat and no rule can fire. The same holds for the ingest worker and for a single product's worker pool (domain-per-stream, feature 004).
- **Not exercised**: against a running stack (read from code and configuration).

### R23b. FINDING B22 — scheduled jobs never export metrics

- **Observation**: `grep` of `configure_telemetry(` finds four callers — the API's lifespan, the agent worker, the ingest worker and the Telegram channel. `scripts/ops_digest.py`, `followup_sweep.py`, `tool_call_dedup_sweep.py` and `ops_investigate.py` call only `configure_logging()`. The digest and the follow-up sweep call `notify.post_to_team_channel`, which counts each send in `agent_team_channel_notify_total{sink,outcome}`; the `TeamChannelNotifyFailing` rule reads it.
- **Reproduction**: a fresh process that imports the digest script, runs only its startup call (`configure_logging()`), increments `agent_team_channel_notify_total{sink="slack", outcome="error"}` and inspects `opentelemetry.metrics.get_meter_provider()`. **Observed 2026-10-03**: `_ProxyMeterProvider`, no reader, no exporter — the increment is recorded nowhere.
- **Consequence**: the alert added 2026-10-01 to make "a human could go unnotified" observable cannot fire for the scheduled pushes, which are the notifier's main unattended callers; a sustained failure of the digest push is invisible. Short-lived processes also need a flush at exit, which a periodic exporter alone does not guarantee (not tested).

### R24. FINDING A1 — degrade paths without a counter

- `record_usage` ("usage ledger write failed; continuing without recording"), `reserve_budget` ("tenant_budget_reservation_failed"), `release_budget_reservation` ("tenant_budget_release_failed"), `in_flight_reservation` ("tenant_budget_in_flight_read_failed"), `_tenant_over_daily_budget` ("tenant_budget_check_failed" → returns `False`, i.e. *unenforced*), the detached release task ("tenant_budget_release_task_failed") and `resolve_model` ("model resolution failed; continuing without it") are log lines only; `metrics.py` has no instrument for any.

### R25. FINDING A2 — pricing

- `PRICE_PER_1K_TOKENS_USD` has two entries; `graph_agent_node.py` prices every step by `graph_module.CHAT_MODEL`; `_run_subagent_impl` records the ledger row with `record.model or CHAT_MODEL` (feature 007) — so a specialist that declares its own `model` is priced by two different aliases in two places. The docstring says pointing at a paid provider needs only its alias added; but the table is source code and the alias used in production is `chat`.

### R26. FINDING A3 — the ledger's index and retention (measured)

- Ephemeral `postgres:16-alpine`; the table and index from `03-meter.sql`; 600,000 rows (3 tenants × 200,000, `recorded_at` uniform over 90 days); `ANALYZE`.
  The exact `usage_summary` query for one tenant and the last 24 h (2,223 rows): **Parallel Bitmap Heap Scan** after a **Bitmap Index Scan on `usage_ledger_tenant_principal_idx` returning 200,000 rows**, ~66,000 rows removed by filter per worker, **5,770 buffers, 9.263 ms**. After `CREATE INDEX … (tenant, recorded_at)`: **1,857 buffers, 1.454 ms**, 2,223 rows read.
  Warm cache, one machine; the absolute figures are small and the *shape* — work proportional to a tenant's whole history, on every turn — is the finding. No migration, job or setting trims the table.

### R27. FINDINGS A4–A10

- **A4**: a script cross-checking every instrument against every alert expression and dashboard query found 33 referenced names, **0 undefined**, and 21 defined names referenced by nothing: `agent_circuit_breaker_{opened,rejected,half_open}_total`, `agent_citation_auto_inserted_total`, `agent_context_retrieval_degraded_total`, `agent_deferred_instead_of_acting_total`, `agent_fabricated_tool_output_total`, `agent_misattributed_citations_total`, `agent_missing_ctx_total`, `agent_moderation_ml_degraded_total`, `agent_reference_footer_stripped_total`, `agent_retry_exhausted_total`, `agent_retry_total`, `agent_skipped_required_tool_total`, `agent_subagent_duration_seconds`, `agent_subagent_run_total`, `agent_system_prompt_leak_total`, `agent_tool_retry_total`, `agent_use_skill_without_search_total`, `agent_worker_job_reclaimed_total`, `agent_zero_citations_total`. `_tenant_over_daily_budget` logs the tenant only in the 80% warning branch. `HighTurnErrorRate` selects `outcome="error"` only; `telemetry.py` defines Views for `agent_latency_seconds` and `agent_iterations` only, so `agent_subagent_duration_seconds` uses the SDK defaults.
- **A5**: `Counter.__init__`/`Histogram.__init__` assign `self._labelnames`; `.labels(**kwargs)` passes `kwargs` straight through. A static check of all 53 call sites matched their declarations.
- **A6**: `grep` finds one caller of `_tenant_over_daily_budget` (`astream_events_turn`); `astream_events_resume` and `astream_events_continue_turn` neither call it nor reserve.
- **A7**: `alertmanager.yml` defines `receivers: - name: default` with no configs; `docker-compose.observability.prod.yml` mounts `./observability/alertmanager/alertmanager.yml`.
- **A8**: `grep` of `.env.example` for `MAX_COST_USD_PER_TURN`, `MAX_COST_USD_PER_TENANT_PER_DAY`, `REQUEST_TIMEOUT_SECONDS` finds none; the `agent_requests_total` comment lists `success | rejected | error | timeout` while `_record_turn_metrics` is also called with `"cancelled"`.
- **A9**: no test calls `record_usage` or `usage_summary` directly; `tests/agent/test_usage_ledger.py` uses a fake cursor; no workflow or Makefile target runs `promtool` or validates the dashboards.
- **A10**: `lf.trace(name=name, session_id=session_id, input=input_text)`; `trace.update(output="".join(final_answer))`; the `CallbackHandler` instruments every LLM and tool run; `tests/agent/test_streaming_terminal_events.py::TestTraceOutputMatchesWhatTheClientActuallySaw` asserts the trace shows the answer text; constitution Principle V says traces carry metadata only; pattern 14 says logs never carry content "outside Langfuse".

### R28. What was checked and found sound

- **No metric-name drift**: all 33 names in alerts and dashboards exist; all 53 `.labels(...)` call sites match their declarations.
- **The allowance check's order**: it runs before any graph or tool work, and a refused turn reserves nothing (`TestEntryPointsRefuseBeforeTouchingTheGraph`).
- **Isolation**: every ledger statement filters by tenant; `GET /usage` takes the tenant from the identity.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B17 | Record tokens for timed-out, errored and cancelled turns | Test first; decide the never-resumed paused turn |
| B18 | One process-wide tracing client; shut down at exit | Test first; small |
| B19 | Async or off-loop model resolution with a negative cache | Test first; small |
| B20 | Reset a stale reservation on the next reserve (or per-turn rows) | Test first against a real database |
| B21 | A signal and an alert for a worker-pool outage; readiness reports consumers | New metric and rule; test first |
| B22 | `configure_telemetry` (and an exit flush) in each scheduled script | Test first; small |
| A1 | A counter for each degrade path; an alert on a failed ledger write | Small |
| A2 | Price by resolved model from configuration; warn on unpriced tokens | One decision |
| A3 | `(tenant, recorded_at)` index; retention | New numbered SQL file |
| A4 | Rules for the unwatched degrade metrics; log the tenant at the refusal | Small |
| A5 | Enforce label names | Small |
| A6 | Allowance on resume and continue | Decision: re-check or pre-authorize |
| A7 | A production alert receiver | Deployment decision |
| A8 | `.env.example`, comments | Docs |
| A9 | Real-database and validator tests | Docker for the database ones |
| A10 | Amend the principle or strip content from traces | The constitution process |
| — | Per-person budgets, billing, anomaly detection | Out of scope |
