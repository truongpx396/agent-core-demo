# Feature Specification: The Billing Package Owns Everything Billing

**Feature Branch**: `feat/billing-owns-the-meter` (spec 013)

**Created**: 2026-10-14

**Status**: Proposed, delivered as **one PR** at the owner's explicit request (see "Size" in `plan.md`).

**Input**: "Properly complete the split of the usage-event table in one PR: the agent core must carry nothing about billing or payment,
and the billing package must be ready to reuse in another project with minimal setup."

> **Boundaries.** Builds on specs 010 (credit billing), 011 (the package boundary) and 012 (migrations, idempotency conflicts). It
> does not change the wallet's ledger rules, the webhook intake, the provider adapters or any rule of the constitution. The
> package still imports nothing from the application.
>
> **Not in this feature** (still `packages/billing/docs/ROADMAP.md`): holds as a primitive, outbound notifications, the HTTP service
> shell, turnkey catalog configuration, a build-or-adopt decision, the metric/`usage_event_id` renames.

## Why this is shaped the way it is

The wallet was extracted into a package in spec 011, but the code that **feeds** it stayed in the agent, because it was welded to the
agent's `usage_events` table. Reading the code showed three kinds of billing in the agent core:

1. **In the meter's own write** (`app/agent/usage_events.py`): rating a call in credits (`CREDITS_PER_USD`, `MARKUP`), three billing
   columns added to the agent's table by `postgres-init/21`, the debit in the same transaction, and queuing the event for export to a
   usage-billing provider.
2. **Next to the meter** (`app/billing_integration/`): the export outbox worker (294 lines, reads `usage_events`), and a reconciliation
   (467 lines) that mixes a check the agent owns (do the events agree with the LLM gateway's spend log?) with one billing owns (was
   every event worth credits actually debited?).
3. **On the turn path**: the credit gate in `app/agent/budgets.py` (balance minus in-flight holds, the rate, the failure policy), its
   construction in `runtime.py`, the wallet in `GET /usage`, and about thirty `Settings` fields and the metrics/alerts behind them.

A second project reusing the package would have had to rewrite all of it. The fix is not to move the agent's table: the dollar caps and
`GET /usage` read it with low latency and its columns (model alias, tokens, principal, thread) are the agent's. It is to give the
**package its own generic record of billable usage** and make everything above run on that, with the agent handing over plain facts.

### The flow after this feature

```
agent model call
  └─ usage_events (agent's LLM meter; caps, GET /usage)           ← agent keeps this, with no billing columns
       └─ ONE call, same transaction: billing_port.record_usage_in(conn, Usage(tenant, event_id, kind, cost_usd, model, tokens))
            └─ package: rate → billing_usage_events → debit (savepoint) → billing_export_outbox (savepoint)
```

Two stores, one flow, no shared table (the decision spec 011 D17 recorded). Nothing in the agent names a credit, a rate, a wallet, a
provider or an outbox; the only billing words left are the two port calls and the API's refusal vocabulary
(`ErrorCode.INSUFFICIENT_CREDITS`, the `credits` field of `GET /usage`), which is the contract with API clients.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A second project meters, charges and exports with one call and no billing code (Priority: P1)

A developer installs `credit-billing` in another application, runs `credit-billing-migrate`, sets the provider and rate variables, and
calls `record_usage_in(conn, Usage(...))` from their own meter. They get a rated, idempotent, wallet-debited, export-queued record of the
usage without writing any of those parts.

**Independent test**: with only the package installed (no `app`), against a real Postgres: a tenant with a wallet is debited exactly once
however often the same event is recorded; the same event under another tenant is a different event; a failing debit keeps the event and
is counted; with no rate and no usage-billing provider nothing is written.

**Acceptance scenarios**:

1. **Given** a rate and a tenant with a wallet, **when** an event is recorded twice, **then** one `billing_usage_events` row exists and
   one debit, for the credits `round_half_up(cost × rate × markup, 6)`.
2. **Given** a call with no known cost, **when** recorded, **then** its credits are NULL and nothing is debited (unknown is not free).
3. **Given** the debit raises, **when** recorded, **then** the billing event is kept with its credits, the failure is counted as
   `credit_debit`, and nothing is raised to the caller.
4. **Given** a usage-billing provider is enabled and the tenant is linked to it, **when** recorded, **then** an outbox row exists in
   the same transaction; a replay queues nothing again.
5. **Given** neither a rate nor a usage-billing provider is configured, **when** recorded, **then** no statement is issued.

### User Story 2 - The agent core carries no billing logic (Priority: P1)

**Independent test**: a test scans `app/` and `scripts/` and fails if a module other than `app/billing_integration/` names a credit
column, a rate setting, a wallet table, the outbox, or imports `credit_billing`; the agent's own tests pass with the package's
settings unset.

**Acceptance scenarios**:

1. **Given** the agent's `usage_events`, **when** a fresh database is initialised, **then** it has no credit columns and no outbox table.
2. **Given** `CREDITS_ENFORCEMENT` is off, **when** a turn is checked, **then** the wallet is not read (SC-005 of spec 010 still holds).
3. **Given** enforcement is on and the wallet is empty, **when** a turn is checked, **then** it is refused before any model work, exactly as before.

### User Story 3 - The checks billing owns run in the package, the checks the agent owns stay in the agent (Priority: P2)

1. Export (`credit-billing-export-worker`), the wallet reconciliation (`credit-billing-reconcile`) and the billing-event retention
   (`credit-billing-usage-sweep`) are package commands with the same bounds, metrics and alerts they had.
2. The agent keeps the comparison of its events with the LLM gateway's spend log (`scripts/usage_reconcile.py`), without any wallet.

### Edge Cases

- **A billing write that fails.** The agent's event is already committed, so a lost billing event is lost revenue: it is counted
  (`billing_event_write`), pages (`BillingEventWriteFailing`), and `credit-billing-reconcile` cannot see it (it was never recorded);
  the alert is the defence, and the gateway reconciliation still sees the spend. Disclosed.
- **Existing databases.** `credit-billing-migrate` creates the two new tables; the agent's old `usage_events.credits*` columns and
  `usage_export_outbox` are left in place, unused. Events recorded before the cutover are not copied into the new store: they were
  rated and charged by the old path, and the wallet reconciliation covers events from the cutover on.
- **Pending exports at the cutover.** Unsent rows in the old outbox are not drained by the new worker. An operator who ever enabled a
  usage-billing provider must drain them first (documented in the upgrade note).
- **Event ids.** A usage-billing provider deduplicates by the event id alone, so ids must be unique across tenants of one instance.
- **Order.** Run `make billing-migrate` BEFORE deploying: a missing table is counted, never fatal, and never silent.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The package MUST provide `record_usage_in(conn, usage)` that, in the caller's transaction, rates the usage, stores it in
  `billing_usage_events` keyed `(tenant, event_id)`, debits the wallet, and queues exports, and MUST NOT raise for an operational fault.
- **FR-002**: A repeated `(tenant, event_id)` MUST insert, debit and queue nothing.
- **FR-003**: A fault in the debit MUST roll back only the debit; a fault in queuing MUST roll back only the queuing; a fault in the
  event write MUST roll back all three and be counted.
- **FR-004**: With no rate and no enabled usage-billing provider the function MUST issue no statement.
- **FR-005**: `billing_usage_events` MUST be append-only (UPDATE refused; DELETE refused unless the retention job says so for its own
  transaction) and carry `tenant` on every row and in its primary key.
- **FR-006**: `billing_export_outbox` MUST reference the event by `(tenant, event_id)`, so an export can only ever carry its own
  tenant's event; a finished row is terminal; what a row IS never changes.
- **FR-007**: The package MUST provide the credit gate as `admit(tenant, reserved_usd=...)`, answering allowed / exhausted /
  unavailable / degraded under `CREDITS_ENFORCEMENT` and `CREDIT_CHECK_FAILURE_POLICY`; with enforcement off it MUST read nothing.
- **FR-008**: The package MUST provide the export worker, the wallet reconciliation and the retention sweep as console scripts that use
  its own pool and bootstrap, with the exit codes the other CLIs use.
- **FR-009**: The package's settings class MUST define every variable these need (`CREDITS_PER_USD`, `MARKUP`, `CREDITS_ENFORCEMENT`,
  `CREDIT_CHECK_FAILURE_POLICY`, `BILLING_EXPORT_*`, `CREDIT_RECONCILE_{SETTLE_SECONDS,LOOKBACK_DAYS,INTERVAL_SECONDS}`,
  `BILLING_USAGE_EVENT_RETENTION_DAYS`) with the names, defaults and bounds they had; the host's `Settings` inherits them.
- **FR-010**: The agent's `usage_events` table, `usage_events.py`, `budgets.py`, `runtime.py`, `GET /usage` and `Settings` MUST NOT
  reference rating, wallets, credits columns, the outbox, or `credit_billing`; the host reaches the package only through
  `app/billing_integration/port.py`.
- **FR-011**: The agent MUST keep a gateway-vs-events reconciliation with its own settings (old `CREDIT_RECONCILE_*` names still
  accepted), metrics and alerts, and no wallet.
- **FR-012**: A fresh volume MUST be built from `postgres-init/` with the package's scripts byte-equal to the shipped ones (`24-`, `25-`).
- **FR-013**: Principles I, II and IV hold unchanged: every statement carries the tenant, no tool path reaches the package, every write
  is idempotent.

### Key Entities

- **Usage** (value object): `tenant`, `event_id`, `kind`, `cost_usd` (None = unpriced), `model`, `tokens`.
- **billing_usage_events**: the package's record of billable usage with the rating snapshot (`credits`, `credits_per_usd`, `markup`).
- **billing_export_outbox**: one row per (provider, tenant, event) waiting to reach a usage-billing provider.
- **Admission**: the gate's answer.

## Success Criteria *(mandatory)*

- **SC-001**: A project with only the package installed can meter, charge and export; the package's hermetic and integration suites
  prove it with no `app` on the path.
- **SC-002**: `grep` for credit/rate/wallet/outbox names in `app/` and `scripts/` outside `app/billing_integration/` finds none
  (enforced by a test).
- **SC-003**: Every behaviour of the removed host code has a test in the package (moved, not dropped), and each moved guarantee that a
  fake cannot prove is also proven against a real Postgres.
- **SC-004**: Nothing observable by an operator disappears: every alert and metric that existed still exists under its name, except the
  gateway reconciliation's, renamed for what it is.

## Assumptions and Decisions

- **D1.** A new table, not a moved one (the caps need the agent's table; its columns are the agent's).
- **D2.** The one host-to-package call carries plain facts; rating is the package's. Cost is the agent's, because only it knows the model prices.
- **D3.** `record_usage_in` never raises (the meter outranks billing). Invalid input is counted and logged, not raised, for the same reason.
- **D4.** Metric names are kept (`agent_credit_*`, `agent_usage_export_*`, `agent_cost_governance_degraded_total{path}`); renaming is the roadmap's service feature.
- **D5.** The refusal vocabulary (`ErrorCode.INSUFFICIENT_CREDITS`, `Allowance.status == "insufficient_credits"`, `GET /usage.credits`) stays in the host: it is the API's contract with its clients.
- **D6.** One PR, at the owner's request, though it exceeds the repository's ~1,000-line guideline; the commits are ordered so a reviewer can read the package first.
