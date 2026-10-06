# Contract: Usage Ledger, `GET /usage` and the Daily Allowance

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §2–§4](../data-model.md) | **Constitution**: Principle I (tenant isolation), Principle V (bounded, observable failure)

**Status**: Retrospective — `app/agent/usage_ledger.py`, `app/agent/model_resolver.py`, `app/agent/runtime.py` (`_tenant_over_daily_budget`, `_reserve_turn_budget`, `_release_turn_budget`, `_tenant_budget_envelope`), `app/agent/runtime_stream.py`
(`_record_turn_metrics`, `astream_events_turn`), `app/api/main.py::usage`, `postgres-init/03`, `04`, `12`; tests in `tests/agent/test_usage_ledger.py`, `test_tenant_budget.py`, `test_model_resolver.py`, `tests/api/test_api.py::TestUsage`.
**`record_usage` and `usage_summary` have no direct tests, and no statement runs against a real database (A9).**

## Audience

A **caller** reading its own usage, a **tenant** whose turns can be refused, and an **operator** investigating spend. Identity comes from the standard identity headers (feature 002); no argument names a tenant.

## `GET /usage`

- **Identity**: required (the two identity headers); the tenant is taken from it. There is **no parameter** to name another tenant.
- **Response `200`**, `UsageResponse`: `total_tokens` (all time), `total_cost_usd` (all time), `last_24h_cost_usd` (the same rolling window the allowance uses), `daily_budget_usd` (`MAX_COST_USD_PER_TENANT_PER_DAY`). Two ledger reads per call.
- **Not included**: the in-flight reservation, other tenants, per-principal figures.

## The ledger write — `record_usage(ctx, thread_id, model_alias, total_tokens)`

- **No-ops** without a valid identity, or with `total_tokens ≤ 0`.
- Otherwise computes `cost = total_tokens / 1000 × price(alias)` (`0` for an alias not in the table), resolves the concrete model **best-effort**, and inserts one row.
- **Never raises**: any failure is logged (`usage ledger write failed; continuing without recording`, error *class* only) and the turn proceeds (**A1**: no counter).
- **Called from**: the completed branch of the stream core (**B17**: not from timeout, error or cancel), delegated runs (feature 007, **its B14**), and the two scheduled scripts.
- **`resolve_model(alias)`** does a **synchronous** HTTP GET (5 s timeout) to the proxy's admin endpoint from inside this async function and caches successes only (**B19**).

## The allowance — before any work

`astream_events_turn` (the one new-turn entry point; **not** resume or continue — **A6**):

1. `_allowance_refusal(ctx)` (rule: `budgets.check_allowance`) — `spent` (rolling 24 h) + `reserved` (in-flight, fresh) against `MAX_COST_USD_PER_TENANT_PER_DAY`.
   - at or above: `agent_budget_exceeded_total{scope,window}`++, `agent_requests_total{outcome="rejected"}`++, yield an `error` event with the `TENANT_BUDGET_EXCEEDED` envelope, **return** — the graph is never initialized, nothing is reserved.
   - at or above 80%: `agent_budget_threshold_total{scope,window,threshold}`++, a log line with the tenant and figures, proceed.
   - a failed ledger read: **proceeds** (logged; **unenforced** for this turn — A1).
2. `_reserve_turn_budget(ctx)` — upsert `+MAX_COST_USD_PER_TURN`; returns the amount reserved, `0.0` if the write failed or the identity is invalid.
3. Run the turn.
4. `finally`: a **detached task** calls `release` for the same amount (off the critical path so the thread lock is not held across a database round trip); a failure is logged.

## Reservation semantics

| Property | Behavior |
|----------|----------|
| Granularity | one row per tenant, a running total |
| Amount | the per-turn ceiling, whether or not the model is priced (notional for a free model) |
| Staleness | a row untouched for 5 minutes **reads** as zero |
| Accumulation | the reserve upsert **adds** to the stored value, stale or not — **B20** |
| Release | exact amount, clamped at zero; a crashed worker never releases |

## What a refusal looks like to the caller

The turn's event stream carries one `error` event: `code = TENANT_BUDGET_EXCEEDED`, message "This tenant's daily usage budget has been reached. Please try again later." No partial work, no charge.

## Invariants a change must preserve

1. Every ledger statement filters by tenant; no endpoint lets a caller name one.
2. The allowance check precedes any graph, model or tool work, and a refused turn reserves nothing.
3. Ledger and reservation problems fail **open** — but each must say so with a counter *(not yet true — A1)*.
4. Tokens are always recorded when the identity is valid, whatever the price.
5. A reservation's lifetime is bounded by its turn *(not yet true — B20)*, and every turn's tokens reach the ledger however it ends *(not yet true — B17)*.
