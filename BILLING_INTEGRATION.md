# How the agent app integrates billing

The seam between the agent app and the billing package (`packages/billing/`, importable as `credit_billing`). Everything about billing itself (the wallet, the
webhooks, rating a call in credits, the debit, the export outbox, the wallet reconciliation, the patterns behind them, the runbook) is the package's own and lives
in `packages/billing/`. This file is only the app's side.

**The seam is thin** (spec 013). The agent records its own usage events (`app/agent/usage_events.py`, the table its spend caps read), and:

- hands the package **one plain value per model call**: `billing_port.record_usage_in(conn, Usage(...))`, in the same transaction as the event;
- asks it **one question per turn**: `billing_port.admit(...)`, the credit gate, mapped to a refusal in `app/agent/budgets.py`.

The agent keeps one check of its own: its events against the LLM gateway's spend log (`app/agent/usage_reconcile.py`, `make usage-reconcile`). What remains in
`app/billing_integration/` is `port.py` (the one module host code imports) and `http.py` (the webhook route). Two tests keep the boundary honest, one per side
(`tests/billing/test_billing_core_boundary.py`, `packages/billing/tests/test_boundary.py`), and one scans the agent for any rate, markup, gate or export setting, and
any package table, named outside that directory.

Where to read the rest:

| You want | Read |
|---|---|
| The contract the app calls | [`packages/billing/docs/port.md`](packages/billing/docs/port.md) |
| Why each mechanism is built the way it is (patterns 52-56, 61, 65, 66, B1-B3) and where each lives now | [`packages/billing/docs/PATTERNS.md`](packages/billing/docs/PATTERNS.md) |
| Operating it: turning it on, changing a wallet, reconciling, selling a pack with Stripe or Polar | [`packages/billing/docs/RUNBOOK.md`](packages/billing/docs/RUNBOOK.md) |
| What is not built, in order | [`packages/billing/docs/ROADMAP.md`](packages/billing/docs/ROADMAP.md) |

## How the app finds the package

The package is not in `requirements-lock.txt` (a machine-generated freeze of `requirements.txt`), so there are four ways the app finds it, and which one applies depends on what is running:

| Running | Found through |
|---|---|
| `pytest` and `mypy` in this repo | the root `pyproject.toml`: pytest `pythonpath`, mypy `mypy_path`, ruff `src` |
| anything started by `make` | the Makefile's `export PYTHONPATH` |
| a developer's venv | `-e ./packages/billing` in `requirements-dev.txt` |
| the Docker image, and any CI job that starts the app | `pip install --no-deps ./packages/billing` after the lock install |

The first row is the trap: `pythonpath` is a setting of the pytest PROCESS, so a uvicorn, agent-worker or script process that a test (or a CI step) starts does not inherit it and dies on `from credit_billing.settings import ...` in `app/core/config.py`. That is how PR C went green locally (where `make` and the dev venv supply the package) and red in CI's `test-integration` and `promptfoo`. Every CI job that installs the lock file and runs the app therefore installs the package before it, and `tests/billing/test_ci_installs_the_package.py` reads the workflows and fails if one does not. `test` and `typecheck` are exempt on purpose, because they are the jobs that prove the first row is enough on its own.

## What the app keeps, and the copies it must not let drift

`docker-compose`, Prometheus and Grafana read fixed paths in this repo, so the app carries copies of three things the package owns: `postgres-init/20-`, `22-`, `24-` and `25-` (the
schema scripts), the package's alert rules in `observability/prometheus/alerts.yml`, and the package's dashboard panels in `observability/grafana/dashboards/credit-billing.json`.
The package's files are canonical; `tests/core/test_billing_package_copies.py` fails the build when a copy drifts. Edit the package, then copy.

A schema change reaches an existing database through `credit-billing-migrate` (`make billing-migrate`), and the order matters: **migrate before deploying the code that uses it.**
