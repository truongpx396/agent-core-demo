# Tasks: The Billing Package Owns Everything Billing

One PR, commits in this order. `[P]` = independent of its neighbours. Kinds: docs, edit, new-file, move (`git mv` then adapt), test, verify.

## Phase 1 — Baseline

- [x] T001 verify: record baselines on `main` before any edit: root hermetic and package hermetic counts, `ruff`, `mypy`; the integration tiers (package and host) pass locally.

## Phase 2 — Package: schema, settings, metrics (US1)

- [x] T002 new-file `packages/billing/src/credit_billing/sql/24-billing-usage-events.sql` per data-model.md; byte-equal copy to `postgres-init/24-billing-usage-events.sql`.
- [x] T003 new-file `packages/billing/src/credit_billing/sql/25-billing-export-outbox.sql`; byte-equal copy to `postgres-init/25-billing-export-outbox.sql`.
- [x] T004 edit `packages/billing/src/credit_billing/settings.py`: add the FR-009 fields, validators (six places, enforcement needs a rate, backoff cap, floors) and module constants.
- [x] T005 edit `packages/billing/src/credit_billing/metrics.py`: add `Gauge`; move `agent_credit_enforcement_refused_total`, `agent_usage_export_total`, `agent_usage_export_oldest_pending_age_seconds`, `agent_credit_outstanding`, `agent_credit_reconcile_*`, `agent_cost_governance_degraded_total` (names, labels, help unchanged).

## Phase 3 — Package: behaviour (US1, US3)

- [x] T006 new-file `credit_billing/usage.py` (`Usage`, `Recorded`, `record_usage_in`).
- [x] T007 new-file `credit_billing/admission.py` (`Admission`, `admit`).
- [x] T008 move `app/billing_integration/export.py` → `credit_billing/export.py`; adapt to the new tables/settings/metrics.
- [x] T009 new-file `credit_billing/reconcile.py` (uncharged + wallet totals; report; gauges).
- [x] T010 new-file `credit_billing/retention.py`.
- [x] T011 edit `credit_billing/port.py` (`BillingPort`, `InProcessBilling`, `CreditSummary`).
- [x] T012 new-file `credit_billing/cli/export_worker.py`, `cli/reconcile.py`, `cli/usage_sweep.py`; console scripts in `packages/billing/pyproject.toml`.
- [x] T013 edit package `observability/alerts.yml` and dashboard JSON: the moved alerts and panels.

## Phase 4 — Package tests (US1, US3)

- [x] T014 test hermetic: `test_usage_record.py`, `test_admission.py`, `test_export_enqueue.py` + `test_export_verdicts.py` (moved), `test_reconcile.py`, `test_retention.py`, `test_cli_*.py`, `test_settings.py`, extend `test_metrics.py`, `test_schema.py`, `test_boundary.py`.
- [x] T015 test integration: `integration/test_usage_real_postgres.py`, `test_export_real_postgres.py`, `test_reconcile_real_postgres.py`, `test_retention_real_postgres.py`, extend `test_migrate_real_postgres.py` (24/25 on a 20/22-only database).

## Phase 5 — Host (US2)

- [x] T016 edit `app/agent/usage_events.py`, `usage_events_retention.py`; `postgres-init/` (remove 21, 23).
- [x] T017 edit `app/agent/budgets.py`, `runtime.py`, `app/api/routers/usage.py`, `app/billing_integration/port.py` and `__init__.py`.
- [x] T018 move `app/billing_integration/reconcile.py` → `app/agent/usage_reconcile.py` (gateway only); `scripts/credit_reconcile.py` → `scripts/usage_reconcile.py`; delete `scripts/billing_export_worker.py`, `app/billing_integration/export.py`.
- [x] T019 edit `app/core/config.py` (remove moved fields, add `USAGE_RECONCILE_*` with the old names accepted), `app/core/metrics.py`, `.env.example`, `Makefile`, compose files, `observability/prometheus/alerts.yml`, dashboards.
- [x] T020 test: update/move host tests; add the scan test for SC-002; extend `test_billing_package_copies.py` (scripts 24/25, all moved alerts and panels).

## Phase 6 — Docs

- [x] T021 docs: `packages/billing/README.md` (using it from another project), `docs/PATTERNS.md` (B3), `port.md`, `data-model.md`, `design-note.md`, `ROADMAP.md`, root `README.md` (upgrade note, make targets), `BILLING_INTEGRATION.md`, `CLAUDE.md`, `GRAPH_PATTERNS.md` pointer if a pattern moved.

## Phase 7 — Verify and ship

- [x] T022 verify: `ruff`, `mypy`, root + package hermetic, both integration tiers, the clean-venv run, the CI-shape check (subprocess imports, bind-mount permissions).
- [x] T023 verify: mutation checks listed in plan.md.
- [x] T024 verify: the real provider sandbox tier was NOT run: nothing in this change touches an adapter, a webhook or a credential, and the owner did not ask for it in this request (the earlier authorisation was for spec 012).
- [ ] T025 open the PR, report CI to a result; memory.


## Results (filled in as the work landed)

| | Before | After |
|---|---|---|
| Root hermetic suite | 2,490 passed | 2,403 passed (87 fewer: their guarantees moved into the package, below) |
| Package hermetic suite | 766 passed, 13 skipped | 960 passed, 13 skipped (also green in a clean venv that installs only the package, with `app` not importable, on the newest allowed OpenTelemetry SDK) |
| Mutation check on the new guarantees | n/a | 39 mutants, all caught; an identical-text control survived (one first SURVIVED, a negative cost accepted, and got a test with no rate; pattern B3 lists them) |
| Agent core | named rate, markup, gate and export settings, a credit gate, an export worker, a wallet reconciliation, 3 credit columns on `usage_events` | none of those (a scan test enforces it); two port calls |

Disclosed deviations from the plan: the host keeps the agent's cost column as the float it always was (a first draft stored a quantised Decimal and broke every assertion on it, which was the signal the agent's table did not need to change); the gateway reconciliation's metrics, alerts and variables were renamed (the old variable names still work); the package's wallet check keeps the `CREDIT_RECONCILE_*` names; the PR is far past the repository's size guideline at the owner's request.
