# Quickstart: Validate Observability and Cost Governance

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier first.
**Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves the allowance logic, the reservation statements' *shape*, the metric wiring through the real graph, the log format and the correlation id — against fakes. It does **not** prove
> the ledger's own statements, the reservation against a real database, any telemetry resource behavior, the alert rules, or the dashboards (A9) — and it did **not** catch B17–B20 or B22, which Scenarios B17–B20 and B22 below
> reproduce. Those five scenarios are expected to show the defect *as the system stands*.

---

## Tier 1 — Hermetic (no services, ~3 s)

```bash
pytest tests/core/test_metrics.py tests/core/test_logging_config.py \
       tests/agent/test_usage_ledger.py tests/agent/test_tenant_budget.py tests/agent/test_model_resolver.py \
       tests/api/test_health.py "tests/api/test_api.py::TestUsage" \
       tests/agent/test_streaming_terminal_events.py -q
```

**Expected** (observed 2026-10-03): `81 passed`.

| Requirement | Evidence |
|-------------|----------|
| FR-003 outcome, tool and node metrics through the real graph | `tests/core/test_metrics.py` (`TestNodeLevelMetrics`, `TestToolCallbackMetrics`) |
| FR-006 JSON shape, `extra` fields, stringified extras, exceptions; the correlation id in scope, outside scope, reset, nested, explicit-wins, reset on error | `tests/core/test_logging_config.py` (`TestStructlogFormatter`, `TestBindRequestId`) |
| FR-007 no raw tool args or result in the audit log | `test_metrics.py::TestToolCallAuditLog` (`…never_carries_raw_tool_args_or_result`, the correlated start/success/failure lines) |
| FR-008 a trace shows what the client saw (cache hit, normal stream) | `test_streaming_terminal_events.py::TestTraceOutputMatchesWhatTheClientActuallySaw` (and **A10**: this asserts *content* is in the trace) |
| FR-013/FR-014 the cost ceiling uses the price table | `tests/agent/test_graph_integration.py` (a patched huge price) — **not** `record_usage` itself (A9) |
| FR-017 `GET /usage` pass-through | `tests/api/test_api.py::TestUsage` |
| FR-018 allowance: under, at and over the ceiling; invalid ctx; fails open on a ledger read error; the 80% warning; the rolling window scoped to the tenant; spend + reservations | `tests/agent/test_tenant_budget.py::TestTenantOverDailyBudget` |
| FR-018 a refused turn never touches the graph; an under-budget one does not short-circuit | `test_tenant_budget.py::TestEntryPointsRefuseBeforeTouchingTheGraph` |
| FR-019 reserve returns the ceiling or 0.0; release forwards the amount | `test_tenant_budget.py::TestReserveAndReleaseTurnBudget`; `test_usage_ledger.py::TestReserveBudget`, `TestReleaseBudgetReservation` (fake cursor: statement shape only) |
| FR-020 a stale reservation is excluded **at read**, zero when absent, fail-open | `test_usage_ledger.py::TestInFlightReservation` (`…excludes_a_stale_reservation_at_the_query_level`) |
| model resolution: known alias, unknown, cached after success, degrades on connection failure and on an error status | `tests/agent/test_model_resolver.py::TestResolveModel` (**no test for the event-loop blocking or the missing negative cache — B19**) |
| readiness: all healthy, one down, all down, a hung probe bounded | `tests/api/test_health.py::TestCheckDependencies` |

**Not covered here**: FR-009 (B18, B19), FR-012 (B21), FR-015/SC-004 (B17), FR-021/SC-007 (B20), FR-022 (A6), FR-023 (A1), FR-027 (A9), FR-028/SC-011 (B22).

---

## Tier 2 — Real services (Docker)

There is **no** integration test for the ledger, the allowance or the reservation (`tests/integration/` holds queue, cache and crawler tests). `make test-integration` does not touch `usage_ledger` or `tenant_budget_reservations`. Principle VII's known gap (A9).

---

## Scenario B17 — Reproduce: a timed-out turn records no usage (hermetic; expected: it reproduces)

In a **temporary** file under `tests/agent/` (so the autouse mocks apply; do not commit; delete after):

1. `from app.agent import runtime_stream as rs, usage_ledger`; `from app.agent.graph import GraphDeps`; `from app.agent.graph_build import build_graph`; `from tests.conftest import TEST_CTX, metric_value`.
2. A fake model class whose first `async ainvoke` returns `AIMessage(content="", tool_calls=[{"name": "calculator", "args": {"expression": "1+1"}, "id": "c1"}], usage_metadata={"input_tokens": 400, "output_tokens": 100, "total_tokens": 500})` and whose later calls `await asyncio.sleep(5)`.
3. Monkeypatch `usage_ledger.record_usage` with a recorder, and `rs.REQUEST_TIMEOUT_SECONDS` to `1.0`.
4. `graph = build_graph(GraphDeps(llm=<fake>))`; drive `rs._run_graph_stream(graph, {"messages": [HumanMessage("what is 1+1?")], "require_approval": False}, {"configurable": {"thread_id": "t", "ctx": TEST_CTX}, "recursion_limit": 40}, None)` to completion; then `await graph.aget_state(cfg)`.

**Observed 2026-10-03**: terminal event `error`/`timeout`; `state.values["total_tokens"] == 500`; recorder calls `[]`; the `agent_tokens_total` delta `0`. **Fixed when** the recorder is called once with 500. This is the failing test the B17 fix starts with.

## Scenario B18 — Reproduce: tracing clients leak threads (a standalone script; expected: it reproduces)

Nothing leaves the machine: dummy keys and the discard port.

1. `from langfuse import Langfuse`; `import threading, gc, time, logging`; `logging.disable(logging.CRITICAL)`; record `threading.active_count()`.
2. Ten times: `lf = Langfuse(public_key="pk", secret_key="sk", host="http://127.0.0.1:9")`; `lf.trace(name="t", session_id="s", input="x")`; then `Langfuse(public_key="pk", secret_key="sk", host="http://127.0.0.1:9").flush()` — what one turn does (the opener, then the flush in the `finally`). Keep no references.
3. `gc.collect()`; sleep 1.5 s; print the thread count and whether the remaining threads are daemons.
4. Optionally repeat with the keys unset: one `Langfuse()` and one `.trace(...)`.

**Observed 2026-10-03**: start 1 → **61** threads (60 daemon); with no keys, 1 → 4 for a single client. **Fixed when** the count does not grow with the number of turns.

## Scenario B19 — Reproduce: recording usage blocks the event loop (hermetic; expected: it reproduces)

Temporary file, as above.

1. Monkeypatch `httpx.get` with a function that `time.sleep(0.5)` then raises `httpx.ConnectError("x")`; `model_resolver._cache.clear()`.
2. Start a heartbeat task that records the gap between successive `await asyncio.sleep(0.01)` wake-ups.
3. `await usage_ledger.record_usage(TEST_CTX, "t1", "chat", 100)` twice; stop the heartbeat; print the number of `httpx.get` calls, the elapsed time and the largest gap.

**Observed 2026-10-03**: 2 attempts (no negative cache); 1.01 s for the two calls; largest heartbeat gap **1.02 s**. With the real 5 s timeout this is up to 5 s per completed turn. **Fixed when** the gap stays near 10 ms and the second call does not ask again within the cache interval.

## Scenario B20 — Reproduce: a leaked reservation is resurrected (real Postgres; expected: it reproduces)

Needs Docker and a throwaway database; it touches nothing of the project's.

```bash
docker run -d --rm --name spec-pg -e POSTGRES_PASSWORD=x postgres:16-alpine
# once ready, in psql inside the container:
#   create the table exactly as postgres-init/12-tenant-budget-reservations.sql does (minus \connect);
#   1) run the reserve statement from usage_ledger.reserve_budget for tenant 'acme' and amount 0.50 — and never release;
#   2) UPDATE the row's updated_at to now() - interval '10 minutes'; run the read statement (5-minute window);
#   3) run the reserve statement again; run the read statement;
#   4) run the release statement for 0.50; run the read statement.
docker rm -f spec-pg
```

**Observed 2026-10-03**: after step 2 the read returns **0 rows**; after step 3 **1.000000**; after step 4 **0.500000** with nothing running. **Fixed when** step 3 reads 0.50 and step 4 reads 0.

## Scenario B22 — Reproduce: a scheduled job's metrics have no exporter (hermetic; expected: it reproduces)

1. In a fresh Python process: `import scripts.ops_digest`; `from app.core.logging_config import configure_logging; configure_logging()` — the script's `__main__` startup.
2. `from app.core import metrics; metrics.agent_team_channel_notify_total.labels(sink="slack", outcome="error").inc()`.
3. `from opentelemetry import metrics as m; print(type(m.get_meter_provider()).__name__)`.

**Observed 2026-10-03**: `_ProxyMeterProvider` — no reader, no exporter. **Fixed when** the script's startup installs the real provider (the type is `MeterProvider`) and the counter reaches the collector.

## Check — A4 and A5 (read-only scripts; expected: no drift today)

- **Names**: parse `alerts.yml` and every dashboard's `expr` fields; extract `agent_*` and `up`; compare with the instrument names in `metrics.py` (histograms also match `_bucket`, `_sum`, `_count`). **Observed**: 33 referenced, **0 undefined**; 21 instruments referenced by nothing.
- **Labels**: walk the AST of `app/` and `scripts/` for `.labels(...)` calls on a declared instrument and compare the keyword names with its declared `labelnames`. **Observed**: 53 call sites, **0 mismatches** (A5: nothing enforces this).

## Measure — A3 (Docker; a throwaway Postgres)

Create `usage_ledger` as `postgres-init/03-meter.sql` does; insert 600,000 rows (three tenants, `recorded_at` uniformly over 90 days); `ANALYZE`; run `EXPLAIN (ANALYZE, BUFFERS)` on the `usage_summary` query for one tenant and the last 24 h; then add an index on `(tenant, recorded_at)` and repeat.
**Observed 2026-10-03**: 5,770 buffers and 9.3 ms (a bitmap scan over the tenant's 200,000 index entries) versus 1,857 buffers and 1.5 ms. Warm cache, one machine.

---

## Tier 3 — Full local stack, manual walk-through

1. `make up`, `make restart-all`, `make obs-up` (Grafana on :3300, Prometheus on :9090).
2. Send a few turns through the web UI. **Expected**: Grafana → *Agent overview* shows turn rate and tokens; Loki shows JSON lines sharing one `request_id` per turn.
3. Set a tiny ceiling (`MAX_COST_USD_PER_TENANT_PER_DAY`) **and** a priced model alias (a free model costs $0, so the ceiling never trips); spend it; send another message. **Expected**: the error event "This tenant's daily usage budget has been reached…", `TenantDailyCostBudgetExceeded` pending then firing in Prometheus.
4. `curl` `GET /usage` with the identity headers. **Expected**: your tenant's totals and the limit; no way to name another.
5. Stop **every** agent worker and send a message. **Expected (intended)**: an alert. **As built**: an error after about 30 s and no metric change (B21).

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| A tenant is refused although its real spend is $0 | Reservations count the per-turn ceiling for every turn, priced or not, and a leaked one persists (B20) |
| The ledger is empty after timeouts or errors | B17 — only completed turns are recorded |
| A worker's thread count climbs with traffic | B18 |
| Throughput stalls briefly after each turn when the proxy is slow | B19 |
| The failed-notification alert never fires for the digest | B22 |
| No alert arrives anywhere | The default Alertmanager receiver has no delivery settings (A7) |
| Dashboards are flat and users see "is an agent-worker running" | B21 |
| `MAX_COST_*` seems to have no effect | An unlisted alias costs $0 (A2); also check the setting is not in `.env.example` (A8) |
