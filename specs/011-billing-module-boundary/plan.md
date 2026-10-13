# Implementation Plan: A Self-Contained Billing Module

**Branch**: `docs/011-billing-module-boundary-spec` (PR A's branch carries this spec; B and C get their own) | **Date**: 2026-10-13 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/011-billing-module-boundary/spec.md`. Web research: [research.md](research.md) (R1-R12) and its Phase 0 appendix (P1-P9, from the code).

## Summary

Three behaviour-preserving PRs that turn `app/billing/` into an independent package:

- **PR A, decouple in place.** Billing gets its own settings object, its own metrics and an injected connection provider, so it imports nothing from the host. `reconcile` and `export` (the two modules that read the agent's meter and the gateway) move to a new host package `app/billing_integration/`. A test fails the build if the core imports the host. *Fully designed below; implemented next.*
- **PR B, the port.** The host reaches billing through `app.billing.port` (plus the webhook router, which moves into the core as `app.billing.http`). The contract is written as the full service API; only the three operations the host uses today are implemented. The design note for a future service lands here.
- **PR C, extract.** `packages/billing/` with its own `pyproject.toml`, tests, canonical schema scripts, alert rules, dashboard, scripts and docs; a CI job installs it alone with `app` absent.

No new behaviour, setting, schema or metric name anywhere (FR-021). Follow-ons (012 holds and the service-side event store, 013 the service shell, 014 turnkey) are separate specs (D17).

## Technical Context

**Language/Version**: Python 3.13 (Dockerfile and CI; ruff `target-version = py311`)

**Primary Dependencies**: pydantic-settings (settings), psycopg 3 + psycopg_pool (the host's pool), `opentelemetry-api` (the core's metrics), FastAPI (the webhook router; an optional extra once packaged), httpx (adapters)

**Storage**: Postgres `appdata`, one database and one pool (D2). Core-owned tables from `postgres-init/20` and `22`; host-owned `19`, `21`, `23`. **No schema change.**

**Testing**: pytest. `make test` is hermetic. `make test-integration` (real Postgres via testcontainers) runs both locally (Docker Desktop is up, confirmed 2026-10-13 at `/usr/local/bin/docker`, which is not on the Bash tool's PATH: `export PATH=$PATH:/usr/local/bin`) and in CI. The developer's own compose stack is running; no `make clean`/`clear-*`/`restart-all` and no container of theirs is touched.

**Target Platform**: the existing Linux container images and the developer's macOS venv

**Project Type**: a monorepo with one deployable (the agent app) and, after PR C, one installable package

**Performance Goals**: none new. The delegating connection adds one function call per checkout.

**Constraints**: `tenant` stays the only key (D3); metric names, labels and descriptions unchanged (SC-006); every env var, `make` target, route and alert name unchanged; the OTel meter scope string is **kept** in PR A. Verified against a throwaway collector running the repo's exporter config (P6): the Prometheus exporter turns the scope into an `otel_scope_name` label and does not merge, so a changed scope is a new series, and the two alerts on these counters use a bare `increase(...)`

**Scale/Scope**: 13 files and 2,698 lines in `app/billing/` today; 4 host files import it; ~35 test files touch it

## Constitution Check

*GATE: passes before Phase 0; re-checked after Phase 1 (below).*

| Principle | Touched? | Position |
|---|---|---|
| I. Tenant isolation | Yes (every wallet query) | No query changes. The `WHERE tenant = %s` predicates, composite foreign keys and every tenant-isolation test move or stay unchanged (FR-017). The connection provider is injected and **fails closed** when absent (FR-004). |
| II. Mandatory approval | Yes, indirectly | The existing structural test (no module that serves a tool imports `app.billing`) is kept and re-pointed at every new location (`app.billing_integration`, and in PR B `app.billing.port`/`http`). The model still has no path to a balance. |
| III. Fixed, typed tools | No | No tool is added or changed. |
| IV. Exactly-once | Yes | The debit, its idempotency key `(tenant, idempotency_key)`, the savepoint around `_charge` and the webhook inbox are not edited. PR A changes where a connection comes from, never what runs on it; the provider's `async with` still commits on clean exit and rolls back on error (proved by a hermetic test). |
| V. Bounded, observable failure | Yes | An unwired core raises a distinct `BillingNotConfigured` (counted by the existing degrade paths: `_charge` swallows it into `credit_debit`, the gate into `credit_read`). No loop or retry changes. |
| VI. Untrusted content | No | |
| VII. Tests | Yes | See "Verification" below. A hermetic test per new seam; the integration tier is unchanged and runs in CI. A bug-fix rule is not triggered (no bug). |
| VIII. Why-first docs | Yes | Recorded in Complexity Tracking: the boundary pattern is **not** added to `GRAPH_PATTERNS.md` in PR A. |

Architecture constraints: *Schema changes* none. *Configuration*: tunables still live in `Settings` (it inherits the billing fields), and `.env.example` is unchanged. *Observability*: see Complexity Tracking. *Dependencies*: PR A none; PR C adds the package (and regenerates the lock file, never hand-edited).

**Gate result: PASS with two recorded deviations (Complexity Tracking). Post-design re-check: unchanged.**

## Project Structure

### Documentation (this feature)

```text
specs/011-billing-module-boundary/
├── spec.md              # WHAT and WHY (D1-D18)
├── plan.md              # this file
├── research.md          # R1-R12 (web) + P1-P9 (code, appended)
├── data-model.md        # who owns which table, setting, metric, alert, script, test
├── quickstart.md        # how to prove each PR changed nothing
├── contracts/
│   ├── billing-core-boundary.md   # the dependency rule and the three injected seams
│   └── billing-port.md            # the service-API-shaped port (PR B implements three operations)
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source code: PR A (the layout after it)

```text
app/billing/                     # THE CORE (imports only itself and third parties)
├── __init__.py
├── settings.py        NEW  BillingSettings + the constants consumers import (moved out of app/core/config.py)
├── metrics.py         NEW  the 4 core counters on the OTel API (moved out of app/core/metrics.py)
├── db.py              NEW  configure(connection=...), get_connection(), BillingNotConfigured
├── credits.py  webhooks.py  inbox.py  store.py  display.py     # edited: imports only
└── providers/{__init__,base,fake,stripe,polar}.py              # stripe/polar: lazy settings import re-pointed

app/billing_integration/         # THE HOST'S SIDE (NEW package)
├── __init__.py        NEW  wires the core to sql_store.get_connection (idempotent, import-time, see contract)
├── reconcile.py       MOVED from app/billing/ (git mv, no edits in the move commit)
└── export.py          MOVED from app/billing/

app/core/config.py               # Settings now inherits BillingSettings; re-exports the 11 constants unchanged
app/core/metrics.py              # the 4 counters removed
app/agent/{usage_events,budgets}.py, app/api/routers/{usage,billing}.py,
scripts/{credits,billing_inbox_sweep,billing_export_worker,credit_reconcile}.py   # imports only

tests/billing/test_billing_core_boundary.py   NEW  the enforcement (see PR A design)
```

### Source code: after PR C (the destination, decided here, re-verified at the start of PR C)

```text
packages/billing/
├── pyproject.toml                # distribution "credit-billing"; extras: http (fastapi), test
├── src/credit_billing/           # import name (O2): the core, incl. port.py and http.py
├── sql/20-credit-wallet.sql  22-billing.sql        # canonical (O3)
├── tests/                        # unit, contract, provider_sandbox, integration; own containers helper
├── scripts/                      # credits, inbox sweep, job bootstrap (D11)
├── observability/                # alert rules + dashboard for the core's metrics (O4)
└── docs/                         # provider-port contract, data model, port contract, design note, PATTERNS.md
app/billing_integration/          # stays in the host
```

**Structure Decision**: a `packages/` directory in the same repository (D12). The host installs the package editable; nothing else in the repo is a package.

## Decisions on the plan-level questions

- **O1, cost-to-credits rating: stays in the wallet API for 011** (`credits.credits_for_cost`, `credits.Pricing`). Read in the code: `budgets` calls `credits_for_cost` with the gate's rate, `usage_events` builds a `Pricing` snapshot that `debit_in` stores on the transaction. The signature is used by the debit path and its tests, and moving rating behind the port is the 013/014 change (D15, D18). Disclosed as a coupling in the design note.
- **O2, import name and router: `credit_billing`, with the router inside the package.** `billing` is too likely to collide with another project's top-level module; `credit_billing` matches spec 010's name. The webhook router moves into the core as `app.billing.http` in **PR B** (it needs only FastAPI, the core's settings and its metrics, all of which exist after PR A), and becomes `credit_billing.http` in PR C. FastAPI becomes an optional extra (`http`) of the distribution. Changeable until PR C; the rename sweep is mechanical.
- **O3, init order: the package owns the canonical SQL; `postgres-init/20-…` and `22-…` become committed copies guarded by a byte-equality test.** Single-file bind mounts would avoid the copies but change three consumers (dev and prod compose, the deploy rsync, `tests/containers.py`), none of which can be exercised without Docker. Copies change none of them and make "identical schema" true by construction (SC-005). Revisit when 013 replaces init scripts with a migration tool, which makes the question moot.
- **O4, dashboard and alerts: split by who emits the metric, delivered the same way as the SQL** (canonical in the package, copies in the paths Grafana and Prometheus already read, with the existing alert-rule and dashboard tests extended to cover both). `BillingWebhookQuarantined` and `BillingWebhookFailing` and the wallet panels go to the package; everything else stays. Detailed in PR C's re-plan.

## PR A: design (implemented next)

**A1. Settings** (`app/billing/settings.py`). `BillingSettings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)` and the **ten** core fields moved verbatim with their comments: `billing_providers`, `billing_webhook_secrets`, `stripe_api_key`, `polar_access_token`, `polar_environment`, `billing_webhook_max_body_bytes`, `billing_webhook_rate_limit_per_minute`, `billing_webhook_max_attempts`, `billing_refund_hold_hours`, `billing_inbox_retention_days`; the four validators that read only those fields (`_provider_names_are_path_safe`, `_a_blank_credential_is_unset`, `_stripe_key_is_a_server_side_key`, `_every_enabled_provider_has_a_secret`); and the constant `BILLING_INBOX_MIN_RETENTION_DAYS`. A module-level `settings = BillingSettings()` and the eleven constants (`STRIPE_API_KEY`, `POLAR_*`, `BILLING_*`) computed exactly as `config.py` computes them today. `Settings` becomes `class Settings(BillingSettings)`: every field, validator and env name is unchanged, so `Settings()` and every test that builds one keep working, and `config.py` re-exports the eleven constants so every host importer keeps working. The host keeps what it owns: `credits_per_usd`, `markup`, `credits_enforcement`, `credit_check_failure_policy`, the `billing_export_*` and `credit_reconcile_*` fields, and their validators (they cross no core field). Core modules import their constants from `app.billing.settings`; the names they hold stay the same, so tests that patch `webhooks.BILLING_REFUND_HOLD_HOURS` need no edit. **Three string-path patches change** (`app.core.config.STRIPE_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT` in the two adapter tests) because the adapters now read the core's settings.

**A2. Metrics** (`app/billing/metrics.py`). A 25-line `Counter`/`_BoundCounter` (same `.labels(**).inc()` surface and `.name`, because `tests.conftest.metric_value` finds a metric by `counter.name`) on `opentelemetry.metrics.get_meter(...)`, and the four counters moved verbatim with their help text. The meter scope string is **kept as `"app.core.metrics"`** with a comment saying why (P6: scope is an exported label, so changing it forks every series, and `BillingWebhookQuarantined`/`BillingWebhookFailing` are bare `increase()` rules, which cannot count the first sample of a new series). PR C decides whether to change it, and if so pre-creates the known label sets at 0 and audits the two rules. `credits`, `webhooks` and the router switch `from app.core import metrics` to `from app.billing import metrics`. Tests: 12 references in 7 files re-point; the two tests that read `app/core/metrics.py` as text (`test_alert_rules`, `test_credit_dashboard`) scan both files.

**A3. Connection** (`app/billing/db.py`). `ConnectionProvider = Callable[[], AbstractAsyncContextManager[AsyncConnection]]`; `configure(*, connection)`; `get_connection()` is an `@asynccontextmanager` that raises `BillingNotConfigured` when nothing is configured and otherwise does `async with provider() as conn: yield conn`, so an exception raised in the caller's block reaches the provider's `__aexit__` (rollback) and a clean exit commits, exactly as `sql_store.get_connection` does. `credits`, `webhooks` and `inbox` keep the **name** `get_connection` (`from app.billing.db import get_connection`), so `monkeypatch.setattr(credits, "get_connection", ...)` (the suite's guard and four tests) works unchanged.

**A4. The host's side** (`app/billing_integration/`). `__init__.py` calls `billing.db.configure(connection=sql_store.get_connection)`: a function reference, nothing opened, idempotent, so import-time wiring has none of the call-once hazard that makes `configure_telemetry` start-up only. `reconcile.py` and `export.py` arrive by `git mv` in a **commit that contains only the renames** (it is not importable by itself; the next commit fixes imports; the PR is squash-merged). Six host files that import `app.billing` add `import app.billing_integration  # noqa: F401 - wires the core`: `usage_events`, `budgets`, `routers/usage`, `routers/billing`, `scripts/credits`, `scripts/billing_inbox_sweep`. PR B collapses these into the port.

**A5. Enforcement** (`tests/billing/test_billing_core_boundary.py`).
1. *AST scan* of every file under `app/billing/` over **all** import nodes, nested ones included (the two adapters import settings inside a constructor, which a top-level scan misses): any import of `app` or `app.*` other than `app.billing[.*]` fails, naming file, line and import.
2. *Runtime scan* in a subprocess: importing every core module leaves no `app.*` module outside `app.billing` in `sys.modules`.
3. *Guard the guard*: the scan list is non-empty and includes `credits`, `webhooks`, `inbox`, `providers/stripe`, `providers/polar`; the scanner recognises `import app.core.config`, `from app.core import metrics`, `from app import core`, an indented import inside a function, and the prefix trap `app.billingx`; and it accepts `app.billing.*`.
4. *Wiring guard*: every module under `app/` and `scripts/` outside the two packages that imports `app.billing` also imports `app.billing_integration`.
5. *Behaviour*: an unconfigured core raises `BillingNotConfigured`; an exception inside `async with get_connection()` reaches the provider's exit and a clean exit does not; `BillingSettings()` builds and refuses a bad value without echoing it, with `app.core.config` never imported.
The existing T013 test stays and its tool-module list is unchanged (no tool module may import `app.billing_integration` either: added to its scan).

**Verification (what proves "nothing changed").** Before editing: record `pytest --collect-only -q` counts and the full `make test` result on the branch. After: the same selection must be collected and passing, plus the new tests (the delta equals exactly the new tests). `make lint` and `make typecheck` green. Diffs, each empty: the sorted list of `Settings` field names and their env aliases; the sorted list of metric names, labels and descriptions across `app/core/metrics.py` + `app/billing/metrics.py`; `.env.example`; the `make` target list. The integration tier runs before and after, locally (`make test-integration`) and in CI, and PR A is not described as done until CI's `test-integration` is green too.

**Size.** About 450 hand-written lines (settings ~150 of moved text, metrics ~90 of moved text, db ~50, wiring ~25, boundary tests ~150) plus ~730 lines of pure renames, ~40 mechanical test and import edits, and the docs. Within the waived budget; the review aid is the commit order: docs, then pure moves, then edited code, then tests.

**Risks.** (1) Inheritance order of validators between `BillingSettings` and `Settings`: the existing settings tests are the oracle and must pass unedited. (2) A host path forgetting the wiring: the wiring guard. (3) The integration tier is the only proof for the connection indirection against a real database: run it locally before pushing, and again in CI. (4) Scripts and tests that patch `app.core.config.<moved constant>` by string: found by grep (three), no others. (5) `app/core/config.py` importing `app.billing.settings` is a host-to-core import and is the allowed direction; the scan covers the other direction only.

## PR B: plan (tasks and files; re-confirm at its start)

`app/billing/port.py` (new): the `WalletPort` protocol with plain types (`record_usage_in` taking the caller's connection, `account_balance`, `credits_for_cost`) and `Pricing`/`Balance` re-homed as dataclasses; an in-process implementation delegating to `credits`. `app/billing/http.py` (new): the webhook router, **moved** from `app/api/routers/billing.py` by a pure `git mv`, then re-imported (`app.core.config` constants become `app.billing.settings`). Host call sites switch to the port: `usage_events._charge`, `budgets._credit_check`, `routers/usage`. The six `import app.billing_integration` lines collapse into the port. Rule enforced by a second scan: outside `app/billing/` and `app/billing_integration/`, the only billing imports are `app.billing.port` and `app.billing.http`; inside `app/billing_integration/` the adapters may use `credits` reads and the provider registry, a listed exemption that 012/013 removes (events-vs-wallet reconciliation moves to the service). A **text-level scan** of host files for core table names (`billing_customers`, `credit_*`, `billing_webhook_events`) closes the SQL coupling an import scan cannot see; the one hit today, `usage_events._enqueue_export`'s `SELECT … FROM billing_customers`, moves behind a core function (`data-model.md`). A stub substituted for the port runs the debit and the gate in a test. `contracts/billing-port.md` is finalised and the **design note** (`design-note.md`: triggers, wire form per operation, the consistency change, the missing migration tool, the four extensions of D16) is written.

## PR C: plan (tasks and files; re-plan at its start with the decisions above)

1. Pure-move commit: `app/billing/` → `packages/billing/src/credit_billing/`, `tests/billing/` and the billing tests under `tests/{core,integration,provider_sandbox,scripts}` → `packages/billing/tests/`, scripts → `packages/billing/scripts/`.
2. `pyproject.toml`, the rename sweep `app.billing` → `credit_billing` in the host (mechanical), `requirements.txt` editable install, lock file regenerated, Dockerfile copies the package before installing.
3. SQL canonical in the package plus the byte-equality test; alert rules and dashboard split with both test files extended (O3, O4).
4. The package's own job bootstrap and test helper for a Postgres container (D11); the meter scope name changed and verified against a real collector (P6).
5. CI: a `billing-package` job that installs only the package and runs its hermetic tests with `app` absent (FR-016); the collected-count comparison (FR-017).
6. Docs per D14: pattern files, `BILLING_INTEGRATION.md`, `CLAUDE.md` pointer; **the constitution amendment (VIII) lands as its own small PR before this one**.

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| Four counters are defined in `app/billing/metrics.py`, not `app/core/metrics.py` (constitution: "metrics via `app/core/metrics.py`") | The core may not import the host (FR-001, FR-003) | Re-exporting from `app/core/metrics.py` or importing it would reintroduce the host import; injecting a metrics registry is machinery for four counters |
| The boundary pattern is not added to `GRAPH_PATTERNS.md` in PR A (constitution VIII) | D14 keeps billing out of that file and the amendment that lets a package keep its own patterns lands before PR C | Adding it now writes billing content the maintainer asked to remove; the pattern is recorded in `research.md` P1-P9 and the plan until its home exists |
| A 25-line copy of the `Counter` wrapper | The core cannot import the host's | Calling the OTel API directly would change five call sites and the `metric_value` test helper's `.name` contract |
| Two `Settings`-shaped classes read the same `.env` | The core must configure itself (FR-002) | One shared instance would make the core import the host's settings |
