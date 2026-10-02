# Contract: Metrics, Alerts and Dashboards

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §1, §7–§9](../data-model.md) | **Constitution**: Principle V (every degrade path has a counter; alert where a human could be unaware)

**Status**: Retrospective — `app/core/metrics.py`, `app/core/telemetry.py`, `observability/**`, `docker-compose.observability*.yml`; tests in `tests/core/test_metrics.py`, `tests/api/test_health.py`.
**No test validates the alert rules or the dashboards (A9).**

## Audience

An **operator** reading dashboards and alerts, and an **engineer** adding a metric. This is the whole telemetry surface; there is no `/metrics` endpoint on any process.

## Topology

```text
API ─┐
agent-worker (N replicas, one pool per domain) ─┼─ OTLP/HTTP push, every 15 s ─▶ otel-collector :4318
ingest-worker (N replicas) ─┤                                                   └─ Prometheus exporter :8889
channels (telegram) ─┘                                                                   ▲ the ONE scrape target
                                                         Prometheus (15 s scrape, 15 d) ─┘── alerts.yml ─▶ Alertmanager ─▶ receiver "default" (no delivery)
                                                         Grafana (6 provisioned dashboards)   ·   Promtail ─▶ Loki (168 h)
```

- A process calls `configure_telemetry(service_name)` **once at start**: the API's `lifespan`, or a worker's or channel's `__main__`. **Scheduled scripts do not (B22).** Never at import. A second call is a no-op. A blank `OTEL_EXPORTER_OTLP_ENDPOINT` leaves telemetry off and logs `telemetry_disabled`.
- The stack under `observability/` is **optional and separate** (`make obs-up`); no process depends on it. A collector that is down loses at most the unexported window; nothing fails.

## Adding or changing a metric

1. Create it **only** through `Counter`/`Histogram` in `app/core/metrics.py` (`.labels(k=v).inc()` / `.observe(x)`); never import OpenTelemetry elsewhere.
2. **Declare the label names** in the constructor; use a **small closed set** — never a tenant, principal, thread, filename or any text. The wrapper does **not** check call sites against the declaration (A5): a mismatched key silently creates a new series no alert matches.
3. A **histogram needs a bucket View** in `telemetry.py` or it gets the SDK's millisecond-scale defaults (`agent_subagent_duration_seconds` has none — A4).
4. Every **degrade-and-continue path** gets a counter. If it can leave a human unaware of **committed business state** (a failed notification, a degraded dedup store, a failed upload, **a lost ledger row**), it also gets a rule in `alerts.yml` — "a metric nobody alerts on is still silent".
5. Add the metric to the agent-overview dashboard when an operator should watch it, and a row to the data-model catalog.

## Alert rules (13) — what is watched

Error rate, p95 latency, tool error rate, tenant budget exceeded, moderation-block spike, rate-limit spike, retrieval degradation, semantic-cache errors, checkpoint issues, failing team-channel notifications, degraded duplicate-call store, failing uploads, scrape target down.
Windows and thresholds are **illustrative demo defaults** (`alerts.yml` header says to tune them against real traffic).

## What is **not** watched (today)

| Condition | Why nothing fires | Id |
|-----------|-------------------|----|
| **No worker consumes requests** (all, or one product's pool, or the ingest worker) | The only "nobody answered" signal is an `error` event after the first-event deadline; it increments no counter; workers are not scraped; there is no lag or heartbeat metric and no `absent` rule; readiness has no consumer probe | **B21** |
| A turn that times out or errors **and spent tokens** | No usage is recorded, so the allowance and the ledger under-count; the error-rate rule counts `error` only, timeouts surface only via p95 | **B17**, A4 |
| A scheduled job's push to the team channel fails (ops digest, follow-up sweep) | Those scripts never configure telemetry; their counters go to a no-op provider | **B22** |
| A ledger write, allowance read or reservation operation fails | Log lines only | **A1** |
| ML moderation degraded to pattern-only; a circuit breaker opened; jobs reclaimed after a crash; a delegated run failing | The counters exist and are on no rule or panel | **A4** |
| Which tenant tripped the daily ceiling | The counter has no tenant label and the refusal logs none | A4 |

## Delivery

Alertmanager groups by `alertname` (wait 30 s, interval 5 m, repeat 4 h) to receiver `default`, which has **no notification settings** in either the local or the **production** observability compose file. Alerts are visible in Prometheus and Grafana only (**A7**).

## Invariants a change must preserve

1. One scrape target covers every process; adding a replica needs no monitoring change.
2. Telemetry is configured once, at start, never at import, and its absence never fails a process.
3. No label carries a tenant, principal, thread or text.
4. Every metric an alert or dashboard names exists (verified 2026-10-03: 33 referenced, 0 undefined).
5. A new degrade path ships with its counter — and, where a human could be unaware, its rule.
