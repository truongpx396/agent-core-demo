# Implementation Plan: Credit-Based Billing Readiness

**Branch**: `010-credit-billing-readiness` | **Date**: 2026-10-07 | **Spec**: [spec.md](./spec.md)

**Status**: Proposed (forward-looking, unlike 008 and 009, which are retrospective). Nothing below exists yet.

## Summary

Four layers, built in dependency order, each a provider-independent PR (the provider adapters come last and are not part of this feature):

1. **Meter.** One choke point, `app/agent/metering.py::metered_invoke`, through which every chat-model call passes. It already needs to exist for the gateway identity (`gateway.identity_from_config`); it additionally writes one `usage_events` row (US1) and replaces five hand-copied call sites, so a sixth cannot silently skip metering. This is also where follow-ups and compaction (G1) start being counted.
2. **Wallet.** `app/billing/credits.py`: grants, debits, expiry and adjustments as append-only entries with idempotency keys; a debit commits in the same transaction as the event that caused it (US2).
3. **Gate.** A new limit kind in `budgets.check_allowance`: available credits (balance minus in-flight holds, in credits) must be positive when enforcement is on; a distinct `ErrorCode` before any model work (US3).
4. **Seams.** `app/billing/providers/` (the port and an in-repo `fake`), a persistent webhook inbox with a tenant link table and a server-side catalog (US4), and an export outbox with a worker (US5), then reconciliation and an operator CLI (US6).

## Technical Context

**Language/Version**: Python 3.13. **Storage**: Postgres `appdata` (eight new tables, `data-model.md`). **Dependencies**: none new: `psycopg` through `sql_store.get_connection()`, `httpx` for the future adapters. No provider SDK is added by this feature.
**Testing**: hermetic tier by default (fakes), plus the **integration tier** for everything that relies on a real constraint or lock (principle VII): the `usage_events` primary key, `UNIQUE (tenant, idempotency_key)`, the advisory-lock debit under 50 concurrent writers, the inbox `ON CONFLICT`, the outbox `SKIP LOCKED`. **Performance**: one extra `INSERT` per model call (to be measured on a real Postgres in PR 1 and quoted in its description).
**Constraints**: enforcement off by default; credits `NUMERIC`; no shipped `CREDITS_PER_USD`.

## Constitution Check

| Principle | Verdict | How it is met |
|---|---|---|
| I Tenant isolation | **Touched, met by design** | Every table has its own `tenant` column; every statement filters it inside the query. The webhook endpoint cannot carry a `SecurityCtx`, so it resolves the tenant **only** through `billing_customers` (created by the app at checkout), never from a payload field; a contract test posts a payload naming a different tenant and asserts the grant goes to the linked one. No link means quarantine, never a default tenant. |
| II Mandatory approval | **Not touched, stated** | No agent tool is added. Granting, adjusting or refunding credits is an operator CLI or a verified webhook, never a tool call, so the model has no path to a balance. A test asserts no `credit`/`billing` function is registered in `TOOL_CAPABILITIES`. |
| III Fixed, typed tools | **Not touched** | No tool. |
| IV Exactly-once side effects | **Primary; duplicate story below** | Four independent unique keys (event id, `(tenant, idempotency_key)`, inbox `(provider, event_id)`, outbox `(provider, event_id)`), plus the provider-side idempotency key (the event id). |
| V Bounded, observable failure | **Primary** | Every loop has a ceiling (outbox attempts and 30-day age, inbox retries, reconciliation page size). Policies: **event write fails open** (the turn succeeds) with a page; **credit check** follows `CREDIT_CHECK_FAILURE_POLICY` (default `open`, like the budget check, documented). Each degrade path has a metric and the money-hiding ones an alert (`data-model.md`). |
| VI Untrusted content is data | **Touched, met** | A webhook body is untrusted: signature first, then normalize; PII-scrubbed before it is stored; it never reaches a prompt. |
| VII Test discipline | **Primary** | Hermetic by default; real-Postgres tests for every constraint the design leans on; one adapter contract suite; autouse mocks in `tests/conftest.py` for the new event write and credit check on the turn path. |
| VIII Why-first docs, honest gaps | **Touched** | One `GRAPH_PATTERNS.md` entry per PR with its motivating failure; G1 and G2 are disclosed in the README now, not when fixed. |

**Duplicate story (principle IV), as the constitution asks new write paths to state it up front.**

| Duplicate arrives as | Caught by |
|---|---|
| a replayed or continued turn re-deriving the same call | `usage_events.event_id` primary key (`ON CONFLICT DO NOTHING`); the debit is skipped because it is in the same transaction |
| the same grant or debit retried | `UNIQUE (tenant, idempotency_key)` |
| the same webhook delivered twice, or concurrently | inbox `UNIQUE (provider, event_id)`, then the grant's idempotency key `"{provider}:{event_id}"` |
| the same event exported twice | outbox `PRIMARY KEY (provider, event_id)` + `SKIP LOCKED` claim, then the provider's own idempotency key (the event id) |
| a crash between "sent" and "marked sent" | the provider key; this is the one window the database cannot see |

**Residual windows (disclosed, not hidden):** a model call that returns just before a crash and before its event commits is not recorded; the gateway reconciliation (US6) is the detector, not a prevention.

## PR split

CLAUDE.md: one logical change per PR, target ≤ ~400 hand-written lines, ceiling ~1,000. Each row is independently reviewable and shippable behind the off-by-default flag.

| PR | Scope | Depends on | Est. lines (code + tests) |
|----|-------|-----------|---------------------------|
| **0** | This spec; README and `GRAPH_PATTERNS.md` gap disclosure | none | docs |
| **1a** | `usage_events` table, deterministic id (after verifying O-A), `metered_invoke` choke point, wiring the agent node and subagents; metrics, alert, real-Postgres test | 0 | ~450 |
| **1b** | Route follow-ups, compaction and the two cron scripts through the choke point, each with its own ledger row (closes G1) | 1a | ~350 |
| **1c** | Embeddings (G2): plumb the tenant through the nine call sites that take no `ctx` (a context variable would break principle I), read usage from the raw client, attribute at the gateway | 1b | ~450 |
| **2** | Wallet: accounts, lots, transactions, entries; grant, debit, expire, balance, verify; per-tenant lock; real-Postgres concurrency and schema-guard tests. (`adjust` lands with the CLI in PR 6, `clawback` with refunds in PR 4: both are thin variants of the same debit path.) | 1a | ~900 |
| **3** | `CREDITS_PER_USD`/`MARKUP`, charge on event, gating in `budgets`, `INSUFFICIENT_CREDITS`, `GET /usage` balance, holds in credits | 2 | ~400 |
| **4** | Port, `fake` adapter, contract suite, catalog, `billing_customers`, inbox, `POST /billing/webhooks/{provider}` | 2 | ~600; split if over |
| **5** | Export outbox, worker, bounded retries, age expiry, metrics and alerts | 1a, 4 | ~450 |
| **6** | Reconciliation (events vs ledger vs gateway), `scripts/credits.py` operator CLI, runbook | 2, 5 | ~400 |
| **later (blocked)** | One adapter per provider (Stripe, Polar, PayPal), each against **primary docs and a sandbox account**, each passing the contract suite | 4, 5 | n/a |
| **7a** | `make usage-events-carry-over`: copy the ledger's history into the events (additive; run before 7b) (T030a) | 1b | ~350 |
| **7b** | The caps and `/usage` read `usage_events`; refuse the events kill switch (T030b, D15) | 7a | ~500 |
| **7c-1** | Drop the reconciliation's ledger leg (T030c1) | 7b | ~250 |
| **7c-2** | Stop the per-turn `usage_ledger` write and the dual-write test (T030c2) | 7c-1 | ~500 |
| **7c-3** | An events retention sweep; move the holds out of `usage_ledger.py` and delete it (T030c3) | 7c-2 | ~500 |

## Risks accepted

- **A dual write** (`usage_ledger` per turn and `usage_events` per call) exists from PR 1a until PR 7c (from 7b the caps read the events and the ledger is the second record and the way back). Mitigation: a test asserts the two agree per thread, and the reconciliation reports drift. Chosen over a big-bang rewrite of the budgets, which would put the working cost caps at risk.
- **A per-call insert on the hot path.** Measured in PR 1a; the fail-open policy means a slow or failing write cannot stop a turn.
- **Provider semantics.** Only items marked Verified in `research.md` are in the contract. The Open items (R5) gate the adapter PRs, so a wrong assumption cannot reach production through this feature.
- **Product decisions are not mine to default.** O1 to O4 in the spec need an owner; PR 3 ships without a `CREDITS_PER_USD` default so no accidental price goes live.

## Complexity Tracking

Deliberately not built: a provider adapter, a pricing or checkout UI, per-person wallets, multi-currency, invoicing, tax, proration,
dunning, subscription plan management. A per-tenant advisory lock is used for debits instead of row-level locking on lots: simpler to
prove correct, and per-tenant call rates are low. Revisit only if a single tenant's measured debit rate makes the lock the bottleneck.
