# Tasks: Credit-Based Billing Readiness

**Input**: `spec.md`, `plan.md`, `research.md`, `data-model.md`, `contracts/billing-provider-port.md`

**Format**: `- [ ] Tnnn [PRn] Description`. Every implementation task is preceded by its failing test (constitution VII). A box is
ticked only when the PR that does it has merged.

**Every PR's gate** (CLAUDE.md): `make lint`, `make typecheck`, `make test`, plus `make test-integration` for any PR that relies on a
constraint or a lock; a mutation check of each new test (the mutant must fail the intended test); the matching `GRAPH_PATTERNS.md`
entry and README update; its CI result reported, not assumed.

## Phase 0: Specification (PR 0)

- [ ] T001 [PR0] This spec directory, and the README "Not built" and "Known gaps" disclosure for G1 and G2

## Phase 1: Meter (PRs 1a, 1b, 1c) — US1

- [x] T002 [PR1a] **O-A is resolved** (`research.md` R5: `AIMessage.id`, stable across a checkpoint read); this task is now the **pin test**: it fails if a library upgrade changes how the id is assigned, is not unique per call, or differs after a checkpoint re-read
- [x] T003 [PR1a] `postgres-init/19-usage-events.sql`: table, `(tenant, occurred_at)` index, append-only trigger. Header says how an existing volume applies it
- [x] T004 [PR1a] Failing real-Postgres test: the same `event_id` inserted twice yields one row; `UPDATE`/`DELETE` are rejected
- [x] T005 [PR1a] `app/agent/usage_events.py`: deterministic id, `INSERT … ON CONFLICT DO NOTHING`, fail-open with `usage_event_write_failed_total`; unpriced stored as NULL cost and counted
- [x] T006 [PR1a] `app/agent/metering.py::metered_invoke(llm, messages, *, config, kind)` doing identity + call + usage + event; wire the agent node and the subagent path
- [x] T007 [PR1a] Alert `UsageEventWriteFailing`; autouse mock (`usage_event_sink`) in `tests/conftest.py`; an agreement test that the per-call events sum to the running total the ledger row is written from (per turn rather than per thread, since that is what the ledger is written from); the per-call insert measured at p50 0.56 ms / p95 0.79 ms on a pooled local Postgres (loopback: add your network round trip)
- [x] T008 [PR1b] (the ratchet `tests/agent/test_metering_choke_point.py` already exists from PR 1a and lists these four sites; delete its entries as each is routed) Route follow-ups, compaction, `ops_digest` and `followup_sweep` through `metered_invoke` (closes **G1**); a test that fails if a new `llm.ainvoke` appears outside it
- [ ] T009 [PR1c] **Answer O-B** (`OpenAIEmbeddings` returns vectors only, so usage must come from the raw client's `embeddings.create` response), then plumb `ctx` through `embed_text`/`embed_texts` and their nine call sites, meter embeddings as `kind="embedding"` and attribute them at the gateway (closes **G2**), or record precisely why not and keep the gap disclosed

## Phase 2: Wallet (PR 2) — US2

- [x] T010 [PR2] `postgres-init/20-credit-wallet.sql`: lots, transactions (`UNIQUE (tenant, idempotency_key)`), entries
- [x] T011 [PR2] Failing real-Postgres tests: replay a grant/debit/expiry (one effect); consume the sooner-expiring lot first; overdraft booked and repaid by the next grant; `balance == SUM(entries)`; **50 concurrent debits** lose no update
- [x] T012 [PR2] `app/billing/credits.py`: `grant`, `debit` (inside the event's transaction, under `pg_advisory_xact_lock`), `expire_due`, `balance`, `verify`; every call records actor and reason (`adjust` and `clawback` follow with the CLI and refunds)
- [x] T013 [PR2] Structural test that no module that defines or serves an agent tool imports `app.billing` (the model has no path to a balance)

## Phase 3: Charge and gate (PR 3) — US3

- [x] T014 [PR3] Settings `CREDITS_PER_USD` (no shipped default), `MARKUP`, `CREDITS_ENFORCEMENT` (default off), `CREDIT_CHECK_FAILURE_POLICY`; `.env.example` entries. Enforcement without a rate is refused at startup; a rate with more than 6 decimal places is refused (the event row stores it as `NUMERIC(18,6)`)
- [x] T015 [PR3] Charge credits on the event (rate stored on the row; `postgres-init/21-usage-event-credits.sql`); unpriced debits nothing and is counted. The debit runs in the event's transaction inside a **savepoint**, so a wallet fault costs the charge and never the event (decision recorded in `usage_events.py` and data-model.md; alert `CreditDebitFailing`)
- [x] T016 [PR3] New limit kind in `budgets.check_allowance`; `ErrorCode.INSUFFICIENT_CREDITS`; holds expressed in credits; refusal before any model work; `GET /usage` returns the balance. The gate runs after the dollar limits and only for a tenant that has a wallet
- [x] T017 [PR3] Fail-policy tests (wallet unreadable under `open` and `closed`), and a test that enforcement off changes nothing (SC-005: with it off the wallet is never read). Alert `CreditGateUnenforced`

## Phase 4: Port, inbox, webhooks (PR 4) — US4

- [x] T018 [PR4] `app/billing/providers/base.py` (the port), `fake.py` (signs and parses its own payloads), and `tests/billing/contract.py` parameterised over registered adapters (the pure half in `test_provider_contract.py`; the same harnesses run through the real inbox and wallet in the integration tier; registering an adapter without a harness fails a test). The fake declares no capability: the capability-conditional contract tests arrive with the first adapter that declares one (PR 5 for `USAGE_EXPORT`)
- [x] T019 [PR4] `postgres-init/22-billing.sql`: `billing_customers`, `credit_products`, `billing_webhook_events`, plus a trigger that makes `applied`/`ignored` terminal and a unique index that allows one grant per payment
- [x] T020 [PR4] Failing tests first: duplicate and concurrent delivery (one grant); invalid signature (rejected, counted, nothing applied); unlinked customer (quarantined, alerted); a payload naming a different tenant (grant goes to the linked one); refund after spend (negative balance, usage refused). Also: a refund before its purchase is held then applied or quarantined at its deadline; a failure part-way leaves no grant and is bounded; the grant and the inbox status are one transaction
- [x] T021 [PR4] `POST /billing/webhooks/{provider}` with the seven steps in the contract; body-size cap before read; refuses to start when a configured provider has no secret (settings refuse to load, and the lifespan builds the adapters); per-source rate limit
- [x] T022 [PR4] Alerts `BillingWebhookQuarantined` and `BillingWebhookFailing`; PII is kept out of the stored payload by a **whitelist** (only the normalized fields are stored, never the raw body); retention for the inbox (`make billing-inbox-sweep`, floor 30 days, never touches an open or quarantined row)

## Phase 5: Export (PR 5) — US5

- [x] T023 [PR5] `postgres-init/23-usage-export-outbox.sql`; rows created in the event's transaction (in a savepoint, so a failure to queue costs the export of one event and never the event), only for a tenant linked to a `USAGE_EXPORT` provider that the writing process has enabled; terminal-by-trigger; a trigger keeps an event with its own tenant (a composite foreign key was tried and rejected: it needs a second unique constraint on `usage_events`, which breaks `ON CONFLICT (event_id)` under concurrency)
- [x] T024 [PR5] Failing tests: fail twice then succeed (one delivery); two workers (no double send, via `SKIP LOCKED` on a real Postgres); an event past 30 days is `expired`, counted and alerted. Also: a crash between "the provider accepted it" and "marked sent" re-sends and the provider dedupes; a provider that raises or hangs is a bounded retry; an unreported id is not assumed sent; the two-workers test proves `SKIP LOCKED` by showing the sends **overlapped** (a blocked worker would also not double-send, so no-double-send alone cannot tell them apart); the capability-conditional contract tests run for every adapter that declares `USAGE_EXPORT`
- [x] T025 [PR5] `scripts/billing_export_worker.py` + `make billing-export-worker`: batches, exponential backoff with a cap, bounded attempts; alerts `UsageExportStuck` and `UsageExportExpired`, plus `UsageExportFailed` and `UsageExportEnqueueFailing`; a per-call deadline (the batch's locks are held while the provider is called); `--once` for a single pass. Not wired into compose: run it like the other workers

## Phase 6: Reconcile and operate (PR 6) — US6

- [x] T026 [PR6] Reconciliation of events vs `usage_ledger` vs the gateway spend log (by `end_user`), and optionally a provider balance; a test that deletes one event and expects the report to name tenant, day and amount. Also compares events with the wallet (an event worth credits that no debit was booked for: D11's disclosed gap, now named), against a stand-in for `/spend/logs/v2` built from LiteLLM 1.104's own source; an incomplete gateway read is never compared. **The provider-balance comparison is not built** (it was optional: no adapter declares `BALANCE_READ`, so there is nothing to compare against); it lands with the first adapter that does. `scripts/credit_reconcile.py` + `make credit-reconcile[-worker]`; alerts `CreditReconcileDrift` and `CreditReconcileNotCompleting`
- [x] T027 [PR6] `scripts/credits.py` + `make credits`: `grant`, `adjust`, `show` (lots, entries, balance); `--by` and `--reason` required. `show` is read-only and asks for neither. `credits.adjust_in` (positive = a grant whose lot source is `adjustment`; negative = a debit of kind `adjust`, never refused for a short balance, no wallet opened to hold a debt); a retry is safe only with the same `--key`, which is printed
- [x] T028 [PR6] Runbook in `infra/README.md`; Grafana panel for balance, grant and debit rates, export lag. The dashboard is `observability/grafana/dashboards/credit-billing.json`; there is no Postgres datasource, so the balance panel reads a gauge the reconciliation worker sets, and per-tenant balances stay `make credits ... show`

## Later

**T029 needs a sandbox account and cannot be built without one**; T030 does not (it is the same codebase, no third party), and is three PRs because moving the caps' read and retiring the
ledger write in one change would remove the way back for the thing that stops a tenant spending without limit, and because the carry-over is additive and has to be run before the read moves.

- [ ] T029 [adapters] One adapter per provider, written against primary docs (R5 O-C, O-D), passing the contract suite unchanged. **Blocked: it needs a sandbox account and its keys for each provider** (D13: Stripe first, then Polar, PayPal last), which are the operator's to create and are not available to this repository's tooling (`.env` is deny-listed on purpose). What can be verified without one (a webhook signature against the vendor's own library, a request shape against the vendor's published OpenAPI) does not substitute for a real round trip, and the contract suite is silent about a wire format it has never met. Until then the in-repo `fake` is the only adapter.
- [ ] T030a [PR 7a] `make usage-events-carry-over`: copy the `usage_ledger` history older than the first real usage event into `usage_events` (`ledger:<id>` rows, never rated, charged or exported; idempotent; resumable; `--dry-run`, `--tenant`). Additive, changes no behaviour, and is run **before** T030b
- [ ] T030b [PR 7b] The dollar caps and `GET /usage` read `usage_events` instead of `usage_ledger` (`app/agent/spend.py`); refuse `USAGE_EVENTS_ENABLED=false` (it would make every cap read $0); the ledger is **still written**, so reverting the read is one line. Measured: the monthly tenant read is 426 ms at 2M events in the window (opt-in cap; disclosed), and a per-person index was measured and not added (D15)
- [ ] T030c [PR 7c] Stop the per-turn `usage_ledger` write and delete the dual-write test; drop the reconciliation's ledger leg and the `LedgerWriteFailing` alert; replace `usage-ledger-sweep` with an events retention sweep (the job spec D7 promised and nothing built); move the budget holds out of `usage_ledger.py`

## Dependencies

PR 1a → 2 → 3. PR 2 → 4. PR 1a and 4 → 5. PR 2 and 5 → 6. Adapters need 4 and 5. Only PR 3 changes a customer-visible
behaviour, and only when `CREDITS_ENFORCEMENT` is turned on.
