# Data model: who owns what

This feature changes no table, column, setting, metric or alert. It changes **ownership**, and writes it down once. "Core" is the billing module that becomes `packages/billing/` in PR C; "host" is the agent application.

## Schema

| Script | Creates / alters | Owner | Notes |
|---|---|---|---|
| `19-usage-events.sql` | `usage_events` (append-only, one row per model call) | host | The agent's LLM-shaped meter; the dollar caps read it (D15) |
| `20-credit-wallet.sql` | `credit_accounts`, `credit_lots`, `credit_transactions`, `credit_entries` | **core** | Append-only ledger, composite tenant foreign keys, `UNIQUE (tenant, idempotency_key)` |
| `21-usage-event-credits.sql` | adds `credits`, `credits_per_usd`, `markup` to `usage_events` | host | The rating output stored on the host's row (O1) |
| `22-billing.sql` | `billing_customers`, `credit_products`, `billing_webhook_events` (the inbox), unique-grant-per-payment index | **core** | |
| `23-usage-export-outbox.sql` | `usage_export_outbox` | host | Its enqueue reads `billing_customers` (see "SQL coupling") |

A fresh database applies `19, 20, 21, 22, 23` in that order today and after PR C (`21` and `23` follow the core tables they sit beside).

## SQL coupling the import scan cannot see

| Where | What | Direction | Handled in |
|---|---|---|---|
| `app/agent/usage_events.py::_enqueue_export` | `SELECT … FROM billing_customers` | host reads a core table | PR B: a core function behind the port; plus a text-level scan of host files for core table names (allowlist: `app/billing_integration/`) |
| `app/billing_integration/reconcile.py`, `export.py` | read `credit_*` and `usage_events` | host adapter reads both | stays (a listed exemption) until 012/013 moves wallet-side reconciliation into the service |
| `scripts/credits.py` (`show`) | reads `credit_lots`, `credit_entries`, `credit_transactions` | an operator script of the core | moves into the package in PR C |
| `usage_event_id` on `credit_transactions` | plain text reference, **not** a foreign key | core → host id | unchanged on purpose: usage events are trimmed by retention and the wallet must outlive them (D3) |

## Settings (`.env` names unchanged)

| Owner | Fields |
|---|---|
| **core** (`BillingSettings`, `app/billing/settings.py`) | `billing_providers`, `billing_webhook_secrets`, `stripe_api_key`, `polar_access_token`, `polar_environment`, `billing_webhook_max_body_bytes`, `billing_webhook_rate_limit_per_minute`, `billing_webhook_max_attempts`, `billing_refund_hold_hours`, `billing_inbox_retention_days` |
| host (`Settings`, inherits the above) | `credits_per_usd`, `markup`, `credits_enforcement`, `credit_check_failure_policy`, all `billing_export_*`, all `credit_reconcile_*`, `usage_event*`, everything else |

Validators: the four that read only core fields move with them; `_six_places_at_most`, `_enforcement_needs_a_rate`, `_backoff_cap_is_not_below_its_base` and `_usage_events_are_the_spend_the_caps_read` stay.

## Metrics

| Owner | Metric |
|---|---|
| **core** (`app/billing/metrics.py`) | `agent_credit_granted_total{source}`, `agent_credit_overdraft_total`, `agent_credit_debited_total{kind}`, `agent_billing_webhook_total{provider,outcome}` |
| host | `agent_credit_enforcement_refused_total`, `agent_usage_export_total`, `agent_usage_export_oldest_pending_age_seconds`, `agent_credit_outstanding`, `agent_credit_reconcile_max_drift_usd`, `agent_credit_reconcile_total`, and the degrade counter's `credit_debit`, `credit_read`, `export_enqueue` paths |

## Alerts and dashboard

Core-emitted: `BillingWebhookQuarantined`, `BillingWebhookFailing` and the dashboard panels on granted, debited, overdraft and webhook outcomes. Host-emitted: `UsageEventWriteFailing`, `UsageEventTableMissing`, `ModelUsageUnpriced`, `CreditDebitFailing`, `CreditGateUnenforced`, `UsageExport*`, `CreditReconcile*`. They are split in PR C (O4), not before.

## Code

| Owner | Modules |
|---|---|
| **core** | `credits`, `webhooks`, `inbox`, `store`, `display`, `providers/*`, `settings`, `metrics`, `db`; PR B adds `port`, `http` |
| host: `app/billing_integration/` | `reconcile`, `export`, and the wiring in `__init__` |
| host | `usage_events` (the charge and the enqueue), `budgets` (the gate), `routers/usage`, `routers/billing` until PR B moves it |
| core scripts (move in PR C) | `scripts/credits.py`, `scripts/billing_inbox_sweep.py` |
| host scripts | `scripts/billing_export_worker.py`, `scripts/credit_reconcile.py` |
