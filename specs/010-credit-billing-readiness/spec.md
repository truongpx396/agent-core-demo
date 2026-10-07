# Feature Specification: Credit-Based Billing Readiness

**Feature Branch**: `010-credit-billing-readiness`

**Created**: 2026-10-07

**Status**: Proposed. Nothing in this spec is built yet; `plan.md` splits it into reviewable PRs.

**Input**: "Prepare the usage meter so a payment provider (Stripe, PayPal, Polar, …) can be plugged in later for credit-based usage, the way a professional team would."

> **Boundaries.** This builds on feature 008 (the usage ledger, the tenant/person allowance, the gateway attribution and
> backstop). It adds the layers a *credit* product needs on top of a cost meter: a trustworthy **per-call meter**, an
> app-owned **credit wallet** that gates usage, and **provider-agnostic seams** for money coming in (webhooks) and usage
> going out (export). It does **not** choose or implement a provider: no Stripe, PayPal or Polar code lands in this feature,
> because none of it could be verified here without sandbox keys. A provider is a later adapter against the port in
> `contracts/billing-provider-port.md`.
>
> **Non-goals.** Checkout UI or a pricing page; invoicing, tax, proration, dunning; multi-currency (USD only); wallets for
> individual people (a wallet belongs to a **tenant**; the existing per-person limits stay as spend caps); subscription plan management.

## Why this is shaped the way it is

Three facts, each verified against the provider's own docs on 2026-10-07 (`research.md` R1), decide the architecture:

1. **Stripe applies credit grants to an invoice when it finalizes, and Polar "doesn't block usage if the customer exceeds their
   balance".** Neither can answer "may this request run *now*?". So the app must own the authoritative, real-time balance used
   for gating. The provider owns **money** (payments, refunds, invoices); the app owns **entitlement** (what may still be consumed).
2. **Both providers deduplicate usage by a caller-supplied id** (Stripe `identifier`, ≤100 chars, unique within a rolling 24 h+;
   Polar `external_id`). So every usage event needs a **deterministic id** that is identical on every retry and replay.
3. **Stripe accepts a meter event only if its timestamp is within the past 35 days.** So export must be a bounded-retry
   outbox that alarms before events age out, never a fire-and-forget call.

The current meter cannot support this. Read from the code (`research.md` R3): the ledger holds **one row per completed turn**,
with no unique key and **USD cost only**; and **follow-up suggestions, history compaction and embeddings reach the model but
never reach the ledger**, so no dollar ceiling or credit balance could see them.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Every model call is one immutable, idempotent usage event (Priority: P1)

Whatever the call is for (an answer, a follow-up suggestion, history compaction, a subagent, an embedding, a cron script), the
app records one append-only event with a deterministic id, the tenant, the person, the model, token counts, our cost and a
snapshot of the price used.

**Why this priority**: every later layer (credits, export, reconciliation) is only as honest as this meter, and today it is missing
whole categories of spend.

**Independent Test**: run a turn that produces an answer, follow-ups and a compaction; confirm one event per model call; replay the
same turn from its checkpoint and confirm no event is added.

**Acceptance Scenarios**:
1. **Given** a turn that makes N model calls, **When** it completes, **Then** exactly N events exist, each with a distinct id.
2. **Given** a crash-and-continue that re-derives the same calls, **Then** no event is duplicated.
3. **Given** a call whose model has no known price, **Then** the event is recorded with `cost_usd` NULL, counted as unpriced, never as free.
4. **Given** the event write fails, **Then** the turn still succeeds, the failure is counted, and an alert fires (a lost event is lost revenue).

### User Story 2 - A tenant has a credit balance that usage draws down (Priority: P2)

An operator can grant credits to a tenant (a purchase, a promotion, a manual correction). Usage debits credits, oldest-expiring
first. The balance, the lots it came from and every change are visible and never edited in place.

**Independent Test**: grant, spend, expire and adjust; confirm `balance == sum of entries`, no lot goes negative, and replaying
any grant or debit with the same key changes nothing.

**Acceptance Scenarios**:
1. **Given** two lots, one expiring sooner, **When** usage is debited, **Then** the sooner-expiring lot is consumed first.
2. **Given** a debit larger than the balance, **Then** the shortfall is booked as overdraft, repaid first by the next grant.
3. **Given** the same grant key twice (a replayed webhook), **Then** one grant exists.
4. **Given** 50 concurrent debits on one tenant, **Then** the balance equals the exact sum, with no lost update.

### User Story 3 - A tenant with no credits is refused cleanly (Priority: P3)

When enforcement is on and the available balance (balance minus in-flight holds) is not positive, a turn is refused before any
model work with a distinct code. Enforcement is off by default, so nothing changes until an operator opts in.

**Independent Test**: enable enforcement for a tenant with zero credits; confirm the refusal code, no model call, and that a
grant lets the next turn through.

### User Story 4 - A payment becomes credits exactly once (Priority: P4)

A provider's webhook is verified, recorded once, mapped to a tenant through a link the app created itself, and turned into a grant
from a server-side product catalog (never from an amount in the payload).

**Independent Test**: with a fake provider, deliver the same signed webhook five times, out of order with a refund, and with a bad
signature; confirm one grant, a correct clawback, and a refusal for the forged one.

**Acceptance Scenarios**:
1. **Given** a valid purchase webhook delivered twice, **Then** one grant.
2. **Given** an invalid signature, **Then** it is rejected, nothing is stored as applied, and the rejection is counted.
3. **Given** a customer reference with no tenant link, **Then** the event is quarantined, alerted, and grants nothing.
4. **Given** a refund after the credits were spent, **Then** the balance goes negative (debt) and new usage is refused until repaid.

### User Story 5 - Usage reaches the provider exactly once (Priority: P5)

For a tenant linked to a provider that bills on usage, each event is exported through an outbox with bounded retries, using the
event id as the provider's own idempotency key.

**Independent Test**: fail the provider twice then succeed; confirm one delivery. Hold an event past its provider window; confirm it
is marked expired, counted and alerted instead of silently dropped.

### User Story 6 - An operator can prove the numbers agree (Priority: P6)

A reconciliation compares the app's events, the app's ledger and the gateway's own spend log (which already carries the tenant
as `end_user`), and optionally the provider's balance, and reports drift per tenant per day.

**Independent Test**: delete one event row in a test database; confirm the report names the tenant, day and amount.

### Edge Cases

- A webhook arrives before the checkout's tenant link is committed, or twice concurrently: the inbox's unique key serializes it.
- A purchase and its refund arrive out of order: the refund is held until its purchase is applied, bounded by a deadline.
- The provider is down for longer than its event window: events expire visibly (US5), the wallet is unaffected.
- A model has no price: unpriced is counted and, under the block policy, refused; it never silently costs zero credits.
- The credits-per-dollar rate changes: past events keep the credits they were charged; the rate is stored on each event.
- Two tenants share a person name: wallets, lots and events are keyed by tenant and never joined across it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every model call MUST produce at most one usage event, with an id derived deterministically from the call's identity.
- **FR-002**: Usage events MUST be append-only; no code path updates or deletes one except the retention job.
- **FR-003**: An event MUST record `cost_usd` (NULL when unpriced), token counts, the price snapshot used, and the credits charged.
- **FR-004**: A failed event write MUST NOT fail the turn; it MUST be counted, and a sustained failure MUST page.
- **FR-005**: Every kind of model-spending call (answers, follow-ups, compaction, subagents, embeddings, cron scripts) MUST go through one metering choke point.
- **FR-006**: Credits MUST be exact decimals (`NUMERIC`), never floats.
- **FR-007**: Credit ledger writes MUST be append-only, and idempotent by `(tenant, idempotency_key)`.
- **FR-008**: A debit MUST be recorded in the same transaction as the usage event that caused it, so a replay cannot double-debit.
- **FR-009**: Concurrent debits for one tenant MUST serialize, and the balance MUST always equal the sum of the tenant's entries.
- **FR-010**: Credit enforcement MUST be off by default and opt-in per deployment, and MUST refuse with a distinct code before any model work.
- **FR-011**: The app wallet MUST be the authority for gating; a provider balance MAY be mirrored and reconciled but MUST NOT gate.
- **FR-012**: A provider webhook MUST be signature-verified before anything is stored as applied, and MUST be rejected when the provider is unknown or the signature is invalid.
- **FR-013**: Webhooks MUST be deduplicated by `(provider, event_id)` in a persistent inbox.
- **FR-014**: The tenant for a webhook MUST come from a link the app created, never from webhook payload fields.
- **FR-015**: The credits for a purchase MUST come from the server-side catalog, never from an amount in the payload.
- **FR-016**: An unlinked or unknown customer MUST be quarantined and alerted, never granted to a default tenant.
- **FR-017**: Usage export MUST use the usage event's id as the provider's idempotency key and MUST bound its retries and its age.
- **FR-018**: Every provider adapter MUST pass one shared contract test suite, so a new provider cannot weaken the guarantees above.
- **FR-019**: Every operator action that changes a balance MUST record an actor and a reason.
- **FR-020**: Every degrade path MUST increment a metric, and every path that can hide committed money (a lost event, a stuck webhook, an expired export) MUST have an alert.

### Key Entities

`UsageEvent`, `CreditLot`, `CreditTransaction`, `CreditEntry`, `BillingCustomer` (tenant ↔ provider customer), `CreditProduct`
(server-side catalog), `WebhookInboxEntry`, `ExportOutboxEntry`. Columns and invariants are in `data-model.md`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For any tenant and day, the sum of `usage_events.cost_usd` equals the gateway's spend for that tenant's `end_user` within a stated tolerance, or the reconciliation names the gap.
- **SC-002**: Replaying any usage event, grant, debit or webhook leaves every balance unchanged (shown by tests against a real Postgres, not only fakes).
- **SC-003**: Fifty concurrent debits on one tenant never lose an update or overdraw beyond the in-flight hold.
- **SC-004**: A provider adapter is added without changing the wallet, the inbox or the outbox: only a new adapter module and its contract-test run.
- **SC-005**: With enforcement off, behaviour and latency of a turn are unchanged beyond the event write.

## Assumptions and Decisions

- **D1.** Credits are `NUMERIC(18,6)`: exact, with no per-call rounding bias (integer credits would overcharge every small call).
- **D2.** The app wallet gates; the provider owns money. Providers that hold a balance (Polar meters, Stripe credit balance) are mirrored and reconciled, not trusted for gating.
- **D3.** Wallets are per tenant. Per-person limits remain USD spend caps.
- **D4.** USD only. The conversion is `credits = round_half_up(cost_usd × CREDITS_PER_USD × MARKUP, 6)`, both from settings.
- **D5.** No real provider adapter in this feature; a fake adapter proves the port.
- **D6.** Refund policy default: claw back the credits the refunded purchase granted, allow the balance to go negative, refuse new usage until it is positive. This is a **product decision to confirm** (O2).
- **D7.** Credit entries are a financial record: kept indefinitely by default. Usage events follow `USAGE_EVENT_RETENTION_DAYS`, and the retention job refuses to delete an event that is not yet exported when export is enabled.

- **D8.** A tenant is on credit billing **only if it has a `credit_accounts` row**. No account means never debited and never gated, so the wallet changes nothing
  for an existing tenant until an operator, or a verified purchase, opens one. This is per-tenant opt-in, finer than a global flag, and it is why no
  tenant can be retroactively put into debt by this feature.
- **D9. (closes O3, decided in PR 2.)** Expiry by source, enforced in code (`credits.EXPIRY_REQUIRED`): **promotional credits must expire** (a promo is a
  liability to cap in time) and **subscription credits must expire** (they belong to their period and do not roll over). **Paid credits (`purchase`) do not expire
  unless the caller says so**, because many jurisdictions restrict expiring a prepaid balance someone paid for; that is left to the operator, never a default.

- **D10. (closes O1, decided in PR 3.)** **The app ships no price.** `CREDITS_PER_USD` has no default and `MARKUP` defaults to a neutral 1 (at cost): what a credit is
  worth is the deploying operator's own commercial decision, and a default would put an accidental one live. With no rate set, credits are off for the whole deployment: nothing is
  rated, debited, gated or shown, and every tenant behaves exactly as before. With a rate and `CREDITS_ENFORCEMENT` off, a tenant that has a wallet is still debited (**shadow
  mode**), so an operator can watch balances move before anyone is refused. `CREDITS_ENFORCEMENT` is refused at startup without a rate, and both numbers are limited to the
  six decimal places the event row stores, so a row always reproduces the credits it was charged.
- **D11. (PR 3.)** **The meter outranks the charge.** The debit commits in the event's transaction, but inside a savepoint: if the wallet fails, only the debit is undone and the event is
  kept with the credits it was worth, counted (`credit_debit`) and alerted (`CreditDebitFailing`). One all-or-nothing transaction would turn a wallet outage into a meter outage, and a
  lost event cannot be repaired while an uncharged one can (the debit key is the event id and the row holds every figure it needs). **Disclosed gap:** nothing repairs an uncharged event yet; PR 6's reconciliation names it.

**Product decisions still to be recorded, each in the PR that implements it:** **O2** the refund and chargeback policy in D6 (PR 4); **O4** which provider first (PR 4).
