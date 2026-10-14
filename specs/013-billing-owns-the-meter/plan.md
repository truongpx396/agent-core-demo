# Implementation Plan: The Billing Package Owns Everything Billing

**Branch**: `feat/billing-owns-the-meter` | **Date**: 2026-10-14 | **Spec**: [spec.md](spec.md)

## Summary

Give the package a generic store of billable usage and move everything that runs on it out of the agent: rating, the atomic debit,
export queuing and the export worker, the wallet reconciliation, billing-event retention and the credit gate. The agent keeps its
LLM-shaped `usage_events` (caps, `GET /usage`) and hands the package one plain value per metered call. Delivered as one PR, in commits
ordered package first.

## Technical Context

Python 3.13 (package: >=3.11), psycopg 3 (async), pydantic-settings, OpenTelemetry API, FastAPI (extra). Storage: Postgres, the
`appdata` database. Tests: pytest hermetic with fake connections; the package's `integration` tier with a real Postgres via
testcontainers. Both hermetic suites and both integration tiers run locally (Docker is available) and in CI.

## Constitution Check

| Principle | How this feature meets it |
|---|---|
| I Tenant isolation | `tenant` is in both new tables' keys and in every statement; the outbox references the event by `(tenant, event_id)` so an export cannot carry another tenant's event (the old trigger is replaced by a constraint). The retention and reconciliation jobs span tenants on purpose and are operator commands, never reachable from a request. |
| II No tool path to a balance | The package is still imported only by `app/billing_integration/`; `tests/billing/test_the_wallet_is_not_reachable_by_the_model.py` is kept and extended to the new port functions. |
| IV Exactly once | `(tenant, event_id)` primary key + `ON CONFLICT DO NOTHING`; the debit key is the event id as before; the outbox key is `(provider, tenant, event_id)`; a replay of a recorded event does nothing. |
| VI Observability | Every degrade-and-continue path is counted and alerted: `billing_event_write` (new), `credit_debit`, `export_enqueue`, `credit_read` keep their series; the alerts move with the package and a test keeps the host's copies equal. |
| VII Hermetic default | Moved tests stay hermetic; real-Postgres proofs stay in the `integration` tiers. |
| VIII Disclosure | Gaps listed in spec "Edge Cases" and `ROADMAP.md`. |
| Self-contained packages | The package imports nothing from `app` (boundary tests); the host reaches it only through `app/billing_integration/port.py`; settings are inherited. |

No violation to justify. One deliberate deviation from the repository's PR-size guideline (D6), at the owner's request.

## Design

### The package

| Module | Content |
|---|---|
| `usage.py` (new) | `Usage`, `Recorded`, `record_usage_in(conn, usage)`: validates, rates (`credits_for_cost`), inserts `billing_usage_events ... ON CONFLICT (tenant, event_id) DO NOTHING`, then `_charge` (savepoint) and `_enqueue` (savepoint), all inside one outer savepoint. Never raises. No statement at all when `CREDITS_PER_USD is None` and no enabled provider bills on usage. |
| `admission.py` (new) | `Admission`, `admit(tenant, reserved_usd=<async callable>)`: the gate moved from `budgets._credit_check`, same policy and counters. |
| `export.py` (moved from `app/billing_integration/export.py`) | The outbox worker's logic reading `billing_usage_events` / `billing_export_outbox`; metrics and settings from the package. |
| `reconcile.py` (new, from the wallet half of the host's) | `uncharged` events and wallet totals over a window; report, exit code, gauges, loop. |
| `retention.py` (new, from `usage_events_retention.py`) | Sweep of old billing events; never one whose export is unfinished. |
| `cli/export_worker.py`, `cli/reconcile.py`, `cli/usage_sweep.py` (new) | `credit-billing-export-worker`, `credit-billing-reconcile`, `credit-billing-usage-sweep` on `cli/_runtime.py`. |
| `settings.py` | Gains the fields listed in FR-009 with their validators. |
| `metrics.py` | Gains a `Gauge`, and the counters/gauges that moved (names unchanged, D4). |
| `port.py` | `BillingPort` (was `WalletPort`): `record_usage_in`, `account_balance`, `admit`, `credit_summary`. |
| `sql/24-billing-usage-events.sql`, `sql/25-billing-export-outbox.sql` | The two tables ([data-model.md](data-model.md)). |

### The host

| File | Change |
|---|---|
| `app/agent/usage_events.py` | Inserts its row, then one `billing_port.record_usage_in(conn, Usage(...))` when inserted. `_rating`, `_charge`, `_enqueue_export`, `_INSERT_RATED`, the credit constants go. Failure policy and the paths that remain are unchanged. |
| `app/agent/budgets.py`, `runtime.py` | `CreditGate` is removed; `_credit_check` asks `billing_port.admit` and maps the answer. `runtime._credit_gate` goes. |
| `app/api/routers/usage.py` | `credits` comes from `billing_port.credit_summary`. |
| `app/agent/usage_events_retention.py` | No outbox: simpler statements, no `held_back`. |
| `app/agent/usage_reconcile.py` (from `app/billing_integration/reconcile.py`) | Gateway comparison only. Settings `USAGE_RECONCILE_*` (old `CREDIT_RECONCILE_*` accepted), metrics `agent_usage_reconcile_*`, alerts `UsageReconcile*`, `scripts/usage_reconcile.py`, `make usage-reconcile[-worker]`. |
| `app/billing_integration/` | `export.py` and `reconcile.py` deleted; `port.py` is the one seam. |
| `app/core/config.py`, `app/core/metrics.py` | Billing fields/constants/metrics that moved are removed; the host `Settings` still inherits `BillingSettings`. |
| `postgres-init/` | `21-usage-event-credits.sql` and `23-usage-export-outbox.sql` removed; `24-`, `25-` byte-equal copies added. |
| `scripts/`, `Makefile`, compose, alerts, dashboards, docs | Follow the above. |

### The seam, and why a call and not a registration

A hook the billing side registers would let the agent code carry no import at all, but an entry point that forgot to import the
registration would silently stop billing, which is lost revenue with no error. The existing wiring (`app/billing_integration/__init__`
configures the connection at import, a test scans for a miss) already makes a direct call unforgettable. The agent therefore keeps two
one-line calls through `app/billing_integration/port.py` and nothing else. (A registration is a later option if a host without any
billing is wanted; the port's no-op path already costs it nothing.)

### Compatibility and the cutover (disclosed)

- Old columns/table are left in place on existing databases and unused; a fresh volume does not create them.
- Pending rows in the old `usage_export_outbox` are not drained by the new worker; drain first if a usage-billing provider was ever on.
- Events before the cutover are not copied; the wallet reconciliation covers events from the cutover.
- `credit-billing-migrate` before deploying; a missing table is counted (`billing_event_write`) and paged, never fatal.

## Test matrix

Hermetic (package): record (rating, unpriced, free call, off, duplicate, savepoint structure per fault, invalid input, event-id
tenancy), admission (who is gated, off reads nothing, failure policy, holds lazily read), export (verdicts/backoff moved; enqueue),
reconcile (uncharged query shape, report, exit codes), retention (floor, statements), CLIs (exit codes, DB unreachable), settings
(bounds, defaults), metrics (series), schema scripts (list, order, byte-equal copies), boundary. Hermetic (host): `usage_events`
calls the port once with plain facts and never for a duplicate; budgets map each admission; usage router; the scan test (SC-002);
gateway reconcile (moved tests). Integration (package, real Postgres): record end to end incl. 50 concurrent, replay, rollback of only
the debit, outbox atomicity and tenant constraint, append-only triggers, retention, reconcile findings, export drain, the migrate tool
applies 24 and 25 on a database that has only 20/22, adoption of a host-initialised database. Integration (host): the agent's meter
with the real package, the gate refusing and a grant letting the next turn through, gateway reconcile.

Mutation checks on the new guarantees (a test must fail when the guarded line is removed): the savepoint around the debit, the
`ON CONFLICT` target, the tenant in the key, the off path, the gate's lazy hold read, the retention's unfinished-export guard.

## Size

Large, by decision. Roughly: package source +1,400, package tests +2,600 (largely moved), host source −1,100, host tests −2,300 (moved
out), docs/specs +900, SQL/alerts/dashboards +300. Commits are ordered so the package can be read alone.

## Risks

| Risk | Handling |
|---|---|
| A billing write fails after the agent's event is committed (silent revenue loss) | Counted + paged (`BillingEventWriteFailing`); the gateway reconciliation still sees the spend; migrate-before-deploy documented. |
| A metric/alert silently stops matching after the move | Names kept; a test lists the alert names and the `{path}` values the code emits; the host's copies of the package's alerts are compared. |
| Moved tests lose coverage | Each moved file is `git mv`-ed and adapted, not rewritten; counts before/after are in `tasks.md`. |
| `ON CONFLICT` arbitration (a second unique constraint on the event table) | The event table has exactly one unique constraint, its composite primary key; a test pins it. |
