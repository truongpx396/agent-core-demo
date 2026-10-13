---
description: "Task list for feature 011: a self-contained billing module (three PRs)"
---

# Tasks: A Self-Contained Billing Module

**Input**: `specs/011-billing-module-boundary/` (spec.md, plan.md, research.md, data-model.md, contracts/, quickstart.md)

**Prerequisites**: plan.md, spec.md. Constitution Principles I, II, IV are non-negotiable; the model must still have no path to a balance.

**Organization**: one phase per user story, and each story is one PR (US1 = PR A, US2 = PR B, US3 = PR C). US4 (the design note) is written inside PR B.

**Commit kinds** (FR-020: a move never shares a commit with an edit, so `git diff -M` reviews as renames):
`DOCS` = documentation only · `MOVE` = `git mv` and nothing else (the tree is not importable at that commit; the PR is squash-merged) · `EDIT` = code change · `TEST` = test change.

**Commands**: `source .venv/bin/activate` first. `export PATH=$PATH:/usr/local/bin:/opt/homebrew/bin` (Docker and `gh` are not on the Bash tool's PATH). The developer's compose stack is running: never run `make clean`, `clear-*`, `restart-all`, and never touch a container this feature did not start. Scratch files go in the session scratchpad, not `/tmp`.

## Phase 1: Setup and baseline

- [x] T001 Record the baseline on the branch before any edit, into `$SCRATCH/baseline/` (scratchpad): `pytest --collect-only -q | tail -1`; `make test` summary line; `make test-integration` summary line (Docker is available); the surface snapshot from `quickstart.md` step 0 (sorted `Settings.model_fields`, sorted `agent_*` names in `app/core/metrics.py`, `sha256` of `.env.example`, `sha256` of `make help`). Every later "unchanged" claim is a diff against these files.
- [x] T002 (DOCS) Commit `specs/011-billing-module-boundary/**` (spec.md, plan.md, research.md, data-model.md, contracts/billing-core-boundary.md, contracts/billing-port.md, quickstart.md, tasks.md, checklists/requirements.md) as the first commit of the branch: `docs: spec 011, a self-contained billing module (plan, research, contracts, tasks)`. `.specify/feature.json` is gitignored and stays out.

---

## Phase 2: User Story 1 (P1), PR A. Billing no longer depends on the agent, and a test keeps it that way

**Goal**: `app/billing/` imports only itself and third parties; reconcile and export live on the host side; a test fails the build if that regresses. No behaviour change.

**Independent Test**: add `from app.agent import gateway` to `app/billing/inbox.py`: `tests/billing/test_billing_core_boundary.py` fails naming file and import; remove it: green. Hermetic suite and integration tier pass with the baseline's selection plus only the new tests.

### Commit: pure moves

- [x] T003 [US1] (MOVE) `mkdir app/billing_integration`; `git mv app/billing/reconcile.py app/billing_integration/reconcile.py`; `git mv app/billing/export.py app/billing_integration/export.py`. Commit those two renames and nothing else (`git diff -M --stat HEAD~1` must show both at 100% similarity): `chore: move reconcile and export to the host side (rename only)`.

### Commit: the core stops importing the host (EDIT)

- [x] T004 [P] [US1] (EDIT) Create `app/billing/settings.py`. Move **verbatim, with their comments**, from `app/core/config.py`: the constant `BILLING_INBOX_MIN_RETENTION_DAYS = 30`; class `BillingSettings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)` and the ten fields `billing_providers: str = ""`, `billing_webhook_secrets: dict[str, SecretStr] = Field(default_factory=dict)`, `stripe_api_key: SecretStr | None = None`, `polar_access_token: SecretStr | None = None`, `polar_environment: Literal["sandbox", "production"] = "sandbox"`, `billing_webhook_max_body_bytes: int = Field(default=256 * 1024, ge=1024, le=10 * 1024 * 1024)`, `billing_webhook_rate_limit_per_minute: int = Field(default=600, ge=1)`, `billing_webhook_max_attempts: int = Field(default=5, ge=1)`, `billing_refund_hold_hours: int = Field(default=24, ge=1)`, `billing_inbox_retention_days: int = Field(default=400, ge=BILLING_INBOX_MIN_RETENTION_DAYS)`; and the four validators `_provider_names_are_path_safe`, `_a_blank_credential_is_unset`, `_stripe_key_is_a_server_side_key`, `_every_enabled_provider_has_a_secret` (their messages unchanged, a rejected value never repeated). Then `settings = BillingSettings()` and the eleven constants computed exactly as `config.py` computes them today (`STRIPE_API_KEY`, `POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT`, `BILLING_PROVIDERS` as a tuple, `BILLING_WEBHOOK_SECRETS` as a dict of plain strings, `BILLING_WEBHOOK_MAX_BODY_BYTES`, `BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE`, `BILLING_WEBHOOK_MAX_ATTEMPTS`, `BILLING_REFUND_HOLD_HOURS`, `BILLING_INBOX_RETENTION_DAYS`, `BILLING_INBOX_MIN_RETENTION_DAYS`). It imports nothing from `app`. Module docstring: why the core has its own settings, and that the host's `Settings` inherits it.
- [x] T005 [US1] (EDIT) `app/core/config.py` (after T004): `class Settings(BillingSettings)`; delete the ten fields, the four validators and the constant definition that moved; keep `credits_per_usd`, `markup`, `credits_enforcement`, `credit_check_failure_policy`, every `billing_export_*` and `credit_reconcile_*` field and the validators `_six_places_at_most`, `_enforcement_needs_a_rate`, `_backoff_cap_is_not_below_its_base`, `_usage_events_are_the_spend_the_caps_read`. Replace the eleven constant lines with `from app.billing.settings import (...)  # noqa: F401 - re-exported so every host importer keeps working`. Fix the two stale `app/billing/export.py`/`reconcile.py` path mentions in comments. `Settings.model_fields` (sorted) must equal the baseline.
- [x] T006 [P] [US1] (EDIT) Create `app/billing/metrics.py`: a `Counter`/`_BoundCounter` of about 25 lines on `opentelemetry.metrics.get_meter("app.core.metrics")` with the same `.labels(**).inc()` surface and a `.name` attribute (`tests/conftest.py::metric_value` finds a counter by `.name`), and a comment that the scope string is deliberately unchanged (research P6: scope is an exported `otel_scope_name` label). Move **verbatim, help text included**: `agent_credit_overdraft_total`, `agent_billing_webhook_total` (labels `["provider", "outcome"]`), `agent_credit_granted_total` (label `source`), `agent_credit_debited_total` (label `kind`). Imports only `opentelemetry` and the standard library.
- [x] T007 [US1] (EDIT) `app/core/metrics.py` (after T006): delete those four counters and fix the two stale path mentions in its docstring; every other metric stays untouched.
- [x] T008 [P] [US1] (EDIT) Create `app/billing/db.py` per `contracts/billing-core-boundary.md`: `ConnectionProvider = Callable[[], AbstractAsyncContextManager[AsyncConnection]]`; `class BillingNotConfigured(RuntimeError)`; `configure(*, connection: ConnectionProvider | None) -> None`; `@asynccontextmanager async def get_connection()` that raises `BillingNotConfigured` when unset and otherwise `async with provider() as conn: yield conn`. Long-form docstring: commit on clean exit and rollback on exception come from the provider, unset means refused (Principle I), the core never opens a pool, never commits on a connection it was handed, never holds two connections.
- [x] T009 [US1] (EDIT) `app/billing/credits.py`, `app/billing/webhooks.py`, `app/billing/inbox.py` (after T004, T006, T008): replace `from app.agent.sql_store import get_connection` with `from app.billing.db import get_connection` (**same name**, so `monkeypatch.setattr(credits, "get_connection", ...)` still works); replace `from app.core import metrics` with `from app.billing import metrics` (credits, webhooks); replace `from app.core.config import ...` with `from app.billing.settings import ...` (webhooks: `BILLING_REFUND_HOLD_HOURS, BILLING_WEBHOOK_MAX_ATTEMPTS`; inbox: `BILLING_INBOX_MIN_RETENTION_DAYS`). No other line changes.
- [x] T010 [P] [US1] (EDIT) `app/billing/providers/stripe.py` (line ~120) and `app/billing/providers/polar.py` (line ~117): the **lazy** `from app.core.config import (...)` inside the constructor becomes `from app.billing.settings import (...)`, still lazy ("read at construction, not import: tests re-point the settings").

### Commit: the host's side (EDIT)

- [x] T011 [US1] (EDIT) Create `app/billing_integration/__init__.py`: `from app.agent import sql_store`; `from app.billing import db as billing_db`; `billing_db.configure(connection=sql_store.get_connection)`. Docstring: why import-time wiring is safe for a function reference (opens nothing, idempotent) although `configure_telemetry` must not run at import, and that every host module importing `app.billing` imports this package (enforced by T018). In the moved `app/billing_integration/reconcile.py` and `export.py`: fix only what the move broke (module docstring paths); they keep importing `app.billing.credits`/`providers`/`store`/`display` and the host's `app.core.config`, `app.core.metrics`, `app.agent.sql_store`, `app.agent.gateway`; confirm by grep that neither uses the four moved counters.
- [x] T012 [US1] (EDIT) Add `import app.billing_integration  # noqa: F401 - wires the billing core to the host's connection pool (see its docstring)` to `app/agent/usage_events.py`, `app/agent/budgets.py`, `app/api/routers/usage.py`, `app/api/routers/billing.py`, `scripts/credits.py`, `scripts/billing_inbox_sweep.py`. In `app/api/routers/billing.py` also replace `from app.core import metrics` with `from app.billing import metrics`.
- [x] T013 [P] [US1] (EDIT) `scripts/billing_export_worker.py` and `scripts/credit_reconcile.py`: import `export`/`reconcile` from `app.billing_integration` and fix their stale path mentions. Update the three stale `app/billing/export.py`/`reconcile.py` mentions in `GRAPH_PATTERNS.md` (path text only; no new billing content, D14). Leave `postgres-init/*.sql` and the dashboard JSON untouched (the schema stays byte-identical).

### Commit: tests (TEST)

- [x] T014 [P] [US1] (TEST) Re-point imports and any `"app.billing.export"`/`"app.billing.reconcile"` strings to `app.billing_integration` in `tests/billing/test_credit_reconcile_script.py`, `tests/billing/test_export_verdicts.py`, `tests/billing/test_reconcile_compare.py`, `tests/billing/test_reconcile_gateway.py`, `tests/billing/test_reconcile_report.py`, `tests/integration/test_reconcile_real_postgres.py`, `tests/integration/test_usage_export_real_postgres.py`, `tests/scripts/test_billing_export_worker.py`. `grep -rn "app.billing.export\|app.billing.reconcile\|billing import.*\(export\|reconcile\)" app scripts tests` must then be empty.
- [x] T015 [P] [US1] (TEST) Re-point the three string-path patches to `app.billing.settings.*`: `tests/billing/test_stripe_adapter.py` (`STRIPE_API_KEY`) and `tests/billing/test_polar_adapter.py` (`POLAR_ACCESS_TOKEN`, `POLAR_ENVIRONMENT`).
- [x] T016 [P] [US1] (TEST) Re-point the four core counters' module references (`from app.billing import metrics as billing_metrics`) in `tests/api/test_routes_mounted.py`, `tests/billing/test_webhook_endpoint.py`, `tests/integration/test_billing_webhooks_real_postgres.py`, `tests/integration/test_credit_adjust_and_cli_real_postgres.py`.
- [x] T017 [US1] (TEST) `tests/core/test_alert_rules.py` and `tests/core/test_credit_dashboard.py`: the "defined metrics" scan reads **both** `app/core/metrics.py` and `app/billing/metrics.py` (they read source text; without this both go red).
- [x] T018 [US1] (TEST) Create `tests/billing/test_billing_core_boundary.py` per `plan.md` A5: (1) AST walk of every `app/billing/**/*.py` over **all** import nodes including nested ones, failing on any `app`/`app.*` that is not `app.billing[.*]`, naming file, line, import; (2) a subprocess (`python -I`) imports every core module and asserts no `app.*` module outside `app.billing` is loaded; (3) guard the guard: the scan list is non-empty and includes `credits`, `webhooks`, `inbox`, `providers/stripe`, `providers/polar`, and the scanner recognises `import app.core.config`, `from app.core import metrics`, `from app import core`, an indented import in a function, the prefix trap `app.billingx`, and accepts `app.billing.*`; (4) wiring guard: every file under `app/` and `scripts/` outside `app/billing/` and `app/billing_integration/` that imports `app.billing` also imports `app.billing_integration`, with its own scanner self-test; (5) behaviour: an unconfigured core raises `BillingNotConfigured` (restore the provider after); an exception inside `async with get_connection()` reaches a fake provider's exit while a clean exit does not; `BillingSettings()` builds in a subprocess without `app.core.config` ever imported and refuses a bad `STRIPE_API_KEY` without echoing it.
- [x] T019 [P] [US1] (TEST) `tests/billing/test_the_wallet_is_not_reachable_by_the_model.py`: the scanner also forbids `app.billing_integration` in the tool-module list, with a case in `test_the_scanner_does_recognise_an_import_of_the_wallet`.
- [x] T020 [US1] (TEST) **Not needed (checked, nothing added):** integration tier wiring: the integration tests that exercise `webhooks`/`inbox`/`credits` against the real pool without importing a host module must see a wired core. Run `make test-integration` once, and where `BillingNotConfigured` appears add `import app.billing_integration  # noqa: F401` to `tests/integration/conftest.py` (not to individual tests). **Outcome:** every integration test injects its connection by module-level name (`monkeypatch.setattr(module, "get_connection", ...)`), which the kept name preserves, so none reached the unconfigured core and `tests/integration/conftest.py` is unchanged.

### Verification and shipping

- [x] T021 [US1] Run `make lint`, `make typecheck`, `make test`. Expect the baseline's passed count plus exactly the new tests from T018/T019, and nothing skipped that was not skipped before.
- [x] T022 [US1] Run `make test-integration` locally and compare to the baseline summary; any difference is investigated before pushing, not explained away.
- [x] T023 [US1] `quickstart.md` step 3: diff the surface snapshot against the baseline (Settings fields, `agent_*` metric names across both files, `.env.example`, `make help`). All must be empty.
- [x] T024 [US1] `quickstart.md` step 2 and 4: the red/green probe on `app/billing/inbox.py` (restore it with `git checkout` afterwards) and the `python -I` import check printing `[]`.
- [x] T025 [US1] Mutation check, as this repo does for new guards (report "N mutants, M caught"): (a) a lazy `from app.core.config import X` inside a core function; (b) `get_connection` that swallows the exception (commits instead of rolling back); (c) the wiring import removed from `app/agent/budgets.py`; (d) `get_connection` falling back to a default when unset; (e) the `app.billingx` prefix accepted; (f) `_every_enabled_provider_has_a_secret` dropped from `BillingSettings`. Each must turn a test red; a survivor becomes a test or a disclosed gap.
- [x] T026 [US1] `README.md`: one short statement in the Payments row/section that the billing core imports nothing from the host and where the enforcing test lives; no `GRAPH_PATTERNS.md` entry (Complexity Tracking, D14).
- [ ] T027 [US1] Open the PR: `git push -u origin docs/011-billing-module-boundary-spec`, `gh pr create --base main` titled `chore: the billing core stops importing the host (spec 011 PR A)`. Body: the failure mode (copying `app/billing` into another project fails to import: 7 of 13 files reach back into the host), the root cause, what changed, what is deliberately **not** done (no port yet, no package move, meter scope string unchanged and why, the pre-existing first-sample gap in the two webhook alerts), the baseline/after counts, the mutation result, and the commit order for review. End with the attribution line. Then watch CI: report `gh pr checks <n>`, do not call it done while a check is pending or red, and tell "also red on main" from "NOT red on main".

**Checkpoint**: PR A merged. The core can be read, tested and changed without the agent.


### PR A results (recorded 2026-10-14, before opening the PR)

| Check | Before | After |
|---|---|---|
| Hermetic suite (`make test`) | 2957 passed, 13 skipped | **3003 passed, 13 skipped** = 2957 + the 46 new tests, nothing else changed |
| Integration tier (`make test-integration`, real Postgres via testcontainers) | 338 passed, 3 setup errors (an ml-service image build hit a network `IncompleteRead`, unrelated) | **341 passed, exit 0** (the same 338 plus those 3, which pass when the network does) |
| `make lint`, `make typecheck` | green | green (152 source files) |
| Surface (sorted `Settings` fields, 64 metric names/labels/types, `.env.example` hash, `make help` hash, the 11 constants' names and types) | recorded | **identical** |
| The four moved counters' name, description and labels | in `app/core/metrics.py` | **byte-identical** in `app/billing/metrics.py` (AST comparison against `9b8b99e`); no other host metric changed |
| Mutation check on the new guards | n/a | **9 of 9 caught** (lazy host import, a top-level host import, a host import with no import statement, a connection that commits on error, an unwired core that hands out a connection, a wiring import removed, the `app.billingx` prefix accepted, the settings validator no longer running, the settings no longer hiding a rejected value). One first attempt at the validator mutant was an `IndentationError` and was discarded and redone. |
| Imports of the host from `app/billing/` | 7 of 13 files | **0** |

---

## Phase 3: User Story 2 (P1) and User Story 4 (P3), PR B. The host reaches billing through one port

Re-confirm this phase against the merged state of PR A before starting (it will have moved).

**Independent Test**: substitute a stub for the in-process port in a test, run the debit and the gate through it; a scan finds no other billing import in host code and no core table name in host SQL.

- [ ] T028 [US2] (MOVE) `git mv app/api/routers/billing.py app/billing/http.py` and nothing else.
- [ ] T029 [US2] (EDIT) `app/billing/http.py`: imports only `app.billing.*` and FastAPI (constants from `app.billing.settings`); `app/api/main.py` includes the router from `app.billing.http`; the route, status codes, counters and rate-limit keying unchanged.
- [ ] T030 [P] [US2] (EDIT) Create `app/billing/port.py` per `contracts/billing-port.md`: the `WalletPort` protocol with plain types and the in-process implementation delegating to `credits` (●: `record_usage_in`, `account_balance`, `credits_for_cost`; ○ operations documented, not coded); re-home `Pricing` and `Balance` for it.
- [ ] T031 [P] [US2] (EDIT) `app/billing/store.py`: a function returning the customer links for a tenant and a set of providers, replacing the raw `SELECT … FROM billing_customers` in `app/agent/usage_events.py::_enqueue_export`.
- [ ] T032 [US2] (EDIT) Host call sites go through the port: `app/agent/usage_events.py` (`_charge`, `_enqueue_export`), `app/agent/budgets.py` (`_credit_check`), `app/api/routers/usage.py`. Same SQL effects, same idempotency keys, same savepoint, same `CREDIT_CHECK_FAILURE_POLICY` behaviour. Collapse the six `import app.billing_integration` lines into the port's own import.
- [ ] T033 [US2] (TEST) Extend `tests/billing/test_billing_core_boundary.py`: host code outside `app/billing/` and `app/billing_integration/` imports only `app.billing.port` and `app.billing.http`; a text-level scan finds none of `billing_customers`, `credit_accounts`, `credit_lots`, `credit_transactions`, `credit_entries`, `credit_products`, `billing_webhook_events` in host files outside `app/billing_integration/`; both scanners prove they recognise a violation.
- [ ] T034 [US2] (TEST) A stub port substituted in `tests/billing/test_port_substitution.py` runs `usage_events._charge` and `budgets._credit_check` and nothing else touches billing; the debit still commits or rolls back with the event's transaction (real-Postgres case in `tests/integration/`).
- [ ] T035 [US4] (DOCS) Finalise `contracts/billing-port.md` and write `specs/011-billing-module-boundary/design-note.md`: triggers for a standalone service, each port operation's wire form, the consistency change (in-transaction debit to an at-least-once consume keyed by event id; the balance lags the meter; overdraft already booked as debt; gate behaviour while the wallet is unreachable), the missing migration tool as the first prerequisite, the four extensions of D16, the remaining couplings (D9) and the listed exemption for `app/billing_integration/`.
- [ ] T036 [US2] Verification as T021-T025 (lint, typecheck, test, integration locally, surface diff, mutation check on the two new scans), then the PR `chore: the host reaches billing through one port (spec 011 PR B)` with the same body discipline as T027, and CI watched to a result.

**Checkpoint**: PR B merged. The seam is explicit, scanned, and replaceable.

---

## Phase 4: User Story 3 (P2), PR C. Billing is an independent package in the monorepo

Re-plan this phase against the merged state of PR B before starting; decisions O2-O4 in `plan.md` are the inputs.

**Independent Test**: in a fresh virtual environment with only the package installed (host `app/` absent), its hermetic tests pass; a database built from scratch before and after the move has an identical schema; CI's new job proves the first on every push.

- [ ] T037 [US3] (DOCS) First, as its own small PR: amend constitution VIII through `/speckit-constitution` (a self-contained package records its architectural patterns in its own file) with the Sync Impact Report. PR C depends on it.
- [ ] T038 [US3] (MOVE) `app/billing/` to `packages/billing/src/credit_billing/`; the billing tests (`tests/billing/`, and the billing files under `tests/core/`, `tests/integration/`, `tests/provider_sandbox/`, `tests/scripts/`) to `packages/billing/tests/`; `scripts/credits.py`, `scripts/billing_inbox_sweep.py` to `packages/billing/scripts/`; the pure-rename commit only.
- [ ] T039 [US3] (EDIT) `packages/billing/pyproject.toml` (distribution `credit-billing`, extras `http` and `test`, the dependencies the core really uses, pytest markers), the rename sweep `app.billing` to `credit_billing` across the host (mechanical), `requirements.txt` editable install, `requirements-lock.txt` **regenerated** (never hand-edited), `Dockerfile` copies the package before installing, `pyproject.toml` mypy/ruff/pytest paths, Makefile targets keep their names.
- [ ] T040 [P] [US3] (EDIT) Canonical `packages/billing/sql/20-credit-wallet.sql` and `22-billing.sql`; `postgres-init/20-…` and `22-…` stay as committed copies; a test asserts byte equality; a fresh-database catalog comparison before and after is attached to the PR (SC-005).
- [ ] T041 [P] [US3] (EDIT) Split alerts and the dashboard by emitter (`BillingWebhookQuarantined`/`BillingWebhookFailing` and the wallet panels to the package, copies delivered like the SQL); extend `test_alert_rules`/`test_credit_dashboard` to both files.
- [ ] T042 [P] [US3] (EDIT) The package's job bootstrap (logging, telemetry, flush on exit, D11) with a flush-on-failure test, and its own Postgres container test helper; decide the meter scope string, and if it changes, probe it against a real collector (`research.md` P6), pre-create known label sets at 0 and audit the two `increase()` rules.
- [ ] T043 [US3] (EDIT) CI: a `billing-package` job in `.github/workflows/ci.yml` that installs only the package and runs its hermetic tests with `app` absent; a collected-count comparison (FR-017).
- [ ] T044 [US3] (DOCS) Per D14: move patterns 52, 54, 65, 66 to `packages/billing/docs/PATTERNS.md`, 53, 55, 56, 61 to `BILLING_INTEGRATION.md`, rewrite 59 and 60 generic, leave "moved" index lines (numbers never reused, next is 67); the package README and living docs; the `CLAUDE.md` pointer (and fix its "56 numbered patterns").
- [ ] T045 [US3] Verification as T021-T025 plus the package-alone run, then the PR `chore: billing becomes an independent package (spec 011 PR C)`, CI watched to a result.

**Checkpoint**: PR C merged. `packages/billing/` can be installed and tested alone.

---

## Phase 5: Polish and cross-cutting

- [ ] T046 Update the project memory (`credit-billing-readiness` or a new `billing-module-boundary` entry): PR numbers and states, the Docker/`gh` PATH facts, the scope-label finding, the decisions O1-O4.
- [ ] T047 Open the follow-on specs the roadmap names (D17: 012 the wallet contract with holds and the service-side event store, 013 the service shell, 014 turnkey operation) only when asked; this feature does not start them.

---

## Dependencies and order

- T001 then T002 first. Inside PR A the order is the commit order: T003 (MOVE), T004-T013 (EDIT), T014-T020 (TEST), then T021-T027. Within T004-T013: T005 needs T004; T007 needs T006; T009 needs T004, T006, T008; T010 needs T004; T011-T013 need T009 and T003.
- **Parallel**: T004, T006, T008 (three new files); T010 beside T009; T014, T015, T016, T019 (different files).
- PR B starts only after PR A merges (T028-T036 depend on the merged tree); PR C only after PR B merges and T037 lands.
- US2 depends on US1; US3 depends on US2; US4 is written inside PR B.

## Implementation strategy

**MVP = PR A.** It delivers the enforced boundary, which is the part that makes every later step a move instead of a rewrite, and it is safe to stop there: the core can already be read, tested and changed without the agent. Ship it, watch CI to green, then re-plan PR B against what actually merged.
