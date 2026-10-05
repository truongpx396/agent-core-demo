# Data Model: Observability and Cost Governance

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

Two Postgres tables, one metric catalog, two log shapes, a trace, and the configuration around them. Nothing here is tenant-authored content.

---

## 1. Metric catalog (generated from `app/core/metrics.py` and cross-checked, 2026-10-03)

53 instruments: **50 counters, 3 histograms**. "Alert" lists the rule that reads it; "Dashboard" is the agent-overview dashboard. **21 are on neither** (A4).

| Metric | Type | Labels | Alert | Dashboard |
|--------|------|--------|-------|-----------|
| `agent_requests_total` | Counter | `outcome` (`success`, `rejected`, `error`, `timeout`, `cancelled`) | HighTurnErrorRate | yes |
| `agent_latency_seconds` | Histogram | — | HighTurnLatencyP95 | yes |
| `agent_iterations` | Histogram | — | — | yes |
| `agent_tokens_total` | Counter | — | — | yes |
| `agent_tool_calls_total` | Counter | `tool` | HighToolErrorRate | yes |
| `agent_tool_errors_total` | Counter | — | HighToolErrorRate | yes |
| `agent_human_approval_total` | Counter | `decision` | — | yes |
| `agent_retry_total` | Counter | — | — | — |
| `agent_zero_citations_total` | Counter | — | — | — |
| `agent_misattributed_citations_total` | Counter | — | — | — |
| `agent_reference_footer_stripped_total` | Counter | — | — | — |
| `agent_citation_auto_inserted_total` | Counter | — | — | — |
| `agent_deferred_instead_of_acting_total` | Counter | — | — | — |
| `agent_fabricated_tool_output_total` | Counter | — | — | — |
| `agent_skipped_required_tool_total` | Counter | — | — | — |
| `agent_system_prompt_leak_total` | Counter | — | — | — |
| `agent_retry_exhausted_total` | Counter | — | — | — |
| `agent_tool_budget_exceeded_total` | Counter | — | — | yes |
| `agent_invalid_tool_call_total` | Counter | — | — | yes |
| `agent_use_skill_without_search_total` | Counter | — | — | — |
| `agent_token_budget_exceeded_total` | Counter | — | — | yes |
| `agent_context_retrieval_degraded_total` | Counter | — | — | — |
| `agent_history_compacted_total` | Counter | — | — | yes |
| `agent_capability_gate_total` | Counter | `capability` | — | yes |
| `agent_checkpoint_issue_total` | Counter | `reason` | CheckpointIssues | yes |
| `agent_missing_ctx_total` | Counter | — | — | — |
| `agent_unattended_pause_total` | Counter | — | — | yes |
| `agent_retrieval_degraded_total` | Counter | `stage` | RetrievalDegraded | yes |
| `agent_semantic_cache_total` | Counter | `outcome` | SemanticCacheErrors | yes |
| `agent_ingest_total` | Counter | `source` | — | yes |
| `agent_ingest_refused_total` | Counter | `reason` | — | yes |
| `agent_moderation_total` | Counter | `outcome` | ModerationBlockSpike | yes |
| `agent_worker_job_reclaimed_total` | Counter | `queue`, `outcome` | — | — |
| `agent_tool_dedup_degraded_total` | Counter | — | ToolCallDedupDegraded | — |
| `agent_moderation_ml_degraded_total` | Counter | — | — | — |
| `agent_memory_deletion_total` | Counter | `outcome` | — | yes |
| `agent_no_progress_total` | Counter | — | — | yes |
| `agent_cost_ceiling_exceeded_total` | Counter | — | — | yes |
| `agent_cancellation_total` | Counter | — | — | yes |
| `agent_streaming_cancellation_total` | Counter | — | — | yes |
| `agent_context_window_exceeded_total` | Counter | — | — | yes |
| `agent_rate_limit_exceeded_total` | Counter | — | RateLimitRejectionSpike | yes |
| `agent_budget_exceeded_total` | Counter | `scope`, `window` | TenantBudgetExceeded (scope=tenant) | yes |
| `agent_budget_threshold_total` | Counter | `scope`, `window`, `threshold` (70/85/95) | TenantBudgetNearLimit (scope=tenant, 95) | yes |
| `agent_upload_rejected_total` | Counter | `reason` | — | yes |
| `agent_upload_failed_total` | Counter | `reason` | IngestUploadFailing | — |
| `agent_subagent_run_total` | Counter | `subagent`, `outcome` | — | — |
| `agent_subagent_duration_seconds` | Histogram | `subagent` | — | — |
| `agent_tool_retry_total` | Counter | `dependency` | — | — |
| `agent_circuit_breaker_opened_total` | Counter | `dependency` | — | — |
| `agent_circuit_breaker_rejected_total` | Counter | `dependency` | — | — |
| `agent_circuit_breaker_half_open_total` | Counter | `dependency` | — | — |
| `agent_team_channel_notify_total` | Counter | `sink`, `outcome` | TeamChannelNotifyFailing | — |

**Which processes export** (call `configure_telemetry`): the API, the agent worker, the ingest worker, the Telegram channel. **Which do not**: the ops digest, the follow-up sweep, the duplicate-call sweep and the ops investigation script (**B22**) — their increments go to the no-op proxy provider.

**Absent by design or omission** (nothing to count with): per-tenant or per-principal figures (the ledger answers those), queue depth or consumer lag, a worker heartbeat, a "request given up because no worker answered" counter, a counter for any ledger,
allowance or resolver degrade (**B21, A1**).

**Histogram buckets** (Views in `app/core/telemetry.py`, matched by name): `agent_latency_seconds` — `0.5, 1.0, 2.5, 5, 10, 15, 30, 60, 90, 120` s; `agent_iterations` — `1, 2, 3, 4, 5, 7, 10, 15`. `agent_subagent_duration_seconds` has **no** View and therefore the SDK's
millisecond-scale default buckets — the very problem R4 describes (read from `telemetry.py`; not measured).

## 2. Usage ledger — `usage_ledger` (`postgres-init/03-meter.sql`, `04-resolved-model.sql`)

| Column | Type | Rule |
|--------|------|------|
| `id` | `SERIAL` primary key | |
| `tenant` | `TEXT NOT NULL` | from the identity, never an argument; every read filters by it |
| `principal` | `TEXT NOT NULL` | from the identity |
| `thread_id` | `TEXT NOT NULL` | the conversation; **not unique** — one row per completed turn. Delegated runs use `<parent>:subagent:<name>:<8 hex>` (feature 007); scheduled jobs use their own ids |
| `model_alias` | `TEXT NOT NULL` | `CHAT_MODEL` for a turn; a specialist's alias for a delegated run |
| `total_tokens` | `INTEGER NOT NULL` | always recorded; the row is skipped when ≤ 0 |
| `cost_usd` | `NUMERIC(12,6) NOT NULL` | `total_tokens / 1000 × PRICE_PER_1K_TOKENS_USD.get(alias, 0)` |
| `recorded_at` | `TIMESTAMPTZ NOT NULL DEFAULT now()` | the window's key |
| `resolved_model` | `TEXT` (nullable) | the concrete model behind the alias, best-effort (B19: the lookup is synchronous) |

- **Index**: `(tenant, principal)` only (A3). **Retention**: none (A3). **Uniqueness**: none, deliberately (a turn that really runs twice should record twice).
- **Written by**: `_record_turn_metrics` on the *completed* branch (**B17**), `_run_subagent_impl` (**feature 007 B14**), and the two scheduled scripts (the default tenant, principals `ops-cron` and `sales-followup-cron`).
- **Price table**: `{"gpt-4o": 0.005, "gpt-4o-mini": 0.00015}` USD per 1,000 tokens; any other alias is `0.0` (A2).

### The two reads

| Read | Statement shape | Used by |
|------|-----------------|---------|
| `usage_summary(tenant, principal=None, since=None)` | `SELECT COALESCE(SUM(total_tokens),0), COALESCE(SUM(cost_usd),0) FROM usage_ledger WHERE tenant = %s [AND principal = %s] [AND recorded_at >= %s]` | the allowance check (`since = now − 24 h`), `GET /usage` (twice: all time and the last 24 h) |

## 3. Reservation — `tenant_budget_reservations` (`postgres-init/12-tenant-budget-reservations.sql`)

One row per tenant: `tenant TEXT PRIMARY KEY`, `reserved_usd NUMERIC(12,6) NOT NULL DEFAULT 0`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`.

| Operation | Statement (shape) | Effect |
|-----------|-------------------|--------|
| reserve | `INSERT … VALUES (tenant, amount, now()) ON CONFLICT (tenant) DO UPDATE SET reserved_usd = existing + EXCLUDED.reserved_usd, updated_at = now()` | **adds** to whatever the row holds and refreshes the timestamp |
| release | `UPDATE … SET reserved_usd = GREATEST(reserved_usd − amount, 0), updated_at = now() WHERE tenant = %s` | subtracts, floored at zero, refreshes the timestamp |
| read | `SELECT reserved_usd FROM … WHERE tenant = %s AND updated_at > now() − make_interval(mins => 5)` | a row not touched for 5 minutes reads as **no row** (0.0) |

### Lifecycle, and where it breaks (B20)

| Step | Row after | Read returns |
|------|-----------|--------------|
| turn A reserves 0.50, then its worker is killed | `0.50`, fresh | 0.50 (counted while fresh) |
| tenant idle 10 minutes | `0.50`, 10 min old | **0** (stale, ignored) |
| turn B reserves 0.50 | `1.00`, fresh — the stale 0.50 was **added to**, not replaced | **1.00** |
| turn B releases its own 0.50 | `0.50`, fresh | **0.50, with nothing running** |

The staleness rule is applied on the **read**; the **write** keeps accumulating; nothing resets the row. Each unreleased reservation is permanent. Amount reserved per turn: `MAX_COST_USD_PER_TURN` (0.50), whether or not the model is priced.

## 4. The allowance decision — `_tenant_over_daily_budget(ctx)`

```text
invalid ctx                    → not over (no query)
spent    = usage_summary(tenant, since = now − 24 h).total_cost_usd      (read fails → NOT over, logged; unenforced for this turn)
reserved = in_flight_reservation(tenant)                                 (read fails → 0.0, logged)
projected = spent + reserved
projected ≥ a limit (tenant/day 20.0 always; tenant/month, principal/day, principal/month when > 0)
                                                          → over: agent_budget_exceeded_total{scope,window}++ ; refuse (first exceeded wins, tenant before person)
projected ≥ 70 / 85 / 95 % of a limit                     → proceed: agent_budget_threshold_total{scope,window,threshold}++ (highest crossed) ; log once per day/month with tenant, principal, spent, reserved, limit
else                                                      → proceed
```

A refused turn: `agent_requests_total{outcome="rejected"}`++, an `error` event carrying the `TENANT_BUDGET_EXCEEDED` envelope ("This tenant's daily usage budget has been reached. Please try again later."), **before** the graph is initialized or touched, and **no reservation taken**.
A proceeding turn: `_reserve_turn_budget` → 0.50 (or 0.0 if the write failed) → the turn → a detached `release` task in the `finally`.

Called from: `astream_events_turn` **only** (A6). Not called by resume, crash-continue or the scheduled scripts.

## 5. Log line — `app/core/logging_config.py`

One JSON object per line:

| Key | Source |
|-----|--------|
| `timestamp` | ISO 8601 |
| `level` | lowercase |
| `logger` | the module's logger name |
| `message` | the event text |
| `request_id` | the ambient `bind_request_id` value, unless the call supplied one |
| *(extras)* | every `extra={...}` field, e.g. `node`, `run_id`, `duration_ms`, `error_class`, `tool` |
| `exc_info` | a formatted traceback **string**, only when an exception was attached |

Non-JSON-native extras are stringified. Never present: message text, tool arguments or results, the `state` dict. The id is bound per turn or job (`bind_request_id`) and reset on exit, including on error.

### The per-tool audit lines (`MetricsCallbackHandler`)

`tool_called` (`tool`, `run_id`, `args_fingerprint`), `tool_succeeded` (`run_id`, `result_fingerprint`), `tool_failed` (`run_id`, `error_class`). The fingerprint is the first 16 hex characters of the SHA-256 of the text.

## 6. Trace — optional, per turn and per resume

Opened by `_open_trace(name, session_id, input_text)`: a Langfuse trace whose **input is the user's text**, whose **output is the final answer** (set at the end), plus the LangChain callback's spans (**every prompt, completion, tool input and tool output**).
Tool results arrive credential-scrubbed; the user's text and the answer do not. Flushed in the `finally` of the stream core. **This is content, not metadata only (A10).** One client is built per turn and one more per flush (**B18**).

## 7. Alert rules (`observability/prometheus/alerts.yml`, 13)

| Alert | Expression (abridged) | For | Severity |
|-------|----------------------|-----|----------|
| `HighTurnErrorRate` | `error` outcomes ÷ all outcomes `> 0.05` over 5 m | 5 m | critical |
| `HighTurnLatencyP95` | `histogram_quantile(0.95, …agent_latency_seconds_bucket…) > 30` | 10 m | warning |
| `HighToolErrorRate` | tool errors ÷ tool calls `> 0.1` | 10 m | warning |
| `TenantBudgetExceeded` | `increase(agent_budget_exceeded_total{scope="tenant"}[1h]) > 0` | — | warning |
| `TenantBudgetNearLimit` | `increase(agent_budget_threshold_total{scope="tenant", threshold="95"}[30m]) > 0` | — | warning |
| `ModerationBlockSpike` | blocked moderation outcomes `> 0.5/s` | 5 m | warning |
| `RateLimitRejectionSpike` | rate-limit rejections `> 1/s` | 5 m | warning |
| `RetrievalDegraded` | `rate(agent_retrieval_degraded_total[15m]) > 0` | 15 m | warning |
| `SemanticCacheErrors` | cache `outcome="error"` rate `> 0` | 15 m | warning |
| `CheckpointIssues` | `increase(agent_checkpoint_issue_total[15m]) > 0` | — | warning |
| `TeamChannelNotifyFailing` | `increase(…notify_total{outcome="error"}[15m]) > 0` | 15 m | warning |
| `ToolCallDedupDegraded` | `increase(agent_tool_dedup_degraded_total[15m]) > 0` | 15 m | warning |
| `IngestUploadFailing` | `increase(agent_upload_failed_total[15m]) > 0` | 15 m | warning |
| `ScrapeTargetDown` | `up == 0` | 5 m | critical |

Routing: Alertmanager groups by `alertname` (wait 30 s, interval 5 m, repeat 4 h) to receiver `default`, which has **no delivery settings** (A7). `HighTurnErrorRate` counts the `error` outcome only: a timeout reaches an alert only through the p95 latency rule.

## 8. Dashboards (`observability/grafana/dashboards/`)

`agent-overview.json` (Golden signals; Safety & guardrails; Tools & retrieval; Ingestion, memory & API layer — 29 of the 53 metrics), `logs.json`, `infra-logs.json`, `docker-system-monitoring.json`, `postgres-top10.json`, `redis-top10.json`. Provisioned from `observability/grafana/provisioning/`.

## 9. Health

| Endpoint | Behavior |
|----------|----------|
| `GET /health` | liveness — always 200 while the process is up |
| `GET /health/ready` | each of five probes bounded to 2 s: appdata Postgres, checkpointer Postgres, Qdrant, the queue/cache store, the ML service — 200 only if all pass. **No** probe for the model proxy or for any consumer (B21) |

## 10. Settings and constants

| Name | Value | Where | `.env.example`? |
|------|-------|-------|-----------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` (blank disables) | `Settings` | yes |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | host set; keys **empty** | environment | yes |
| `MAX_COST_USD_PER_TURN` | `0.50` (also the reservation amount) | `Settings` | **no** (A8) |
| `MAX_COST_USD_PER_TENANT_PER_DAY` | `20.0` | `Settings` | **no** (A8) |
| `REQUEST_TIMEOUT_SECONDS` | `60` | `Settings` | **no** (A8) |
| `RESERVATION_STALE_AFTER_MINUTES` | `5` | constant, `usage_ledger.py` | n/a |
| `_TENANT_BUDGET_WARNING_FRACTION` | `0.8` | constant, `runtime.py` | n/a |
| OTLP export interval | 15 s | constant, `telemetry.py` | n/a |
| model-resolver timeout | 5 s | constant, `model_resolver.py` | n/a |
| readiness probe timeout | 2 s | constant, `health.py` | n/a |
| Prometheus retention / Loki retention | 15 d / 168 h | compose / `loki-config.yaml` | n/a |
