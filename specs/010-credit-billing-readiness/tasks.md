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

- [ ] T010 [PR2] `postgres-init/20-credit-wallet.sql`: lots, transactions (`UNIQUE (tenant, idempotency_key)`), entries
- [ ] T011 [PR2] Failing real-Postgres tests: replay a grant/debit/expiry (one effect); consume the sooner-expiring lot first; overdraft booked and repaid by the next grant; `balance == SUM(entries)`; **50 concurrent debits** lose no update
- [ ] T012 [PR2] `app/billing/credits.py`: `grant`, `debit` (inside the event's transaction, under `pg_advisory_xact_lock`), `expire_due`, `adjust`; every call records actor and reason
- [ ] T013 [PR2] Test that no `credit`/`billing` function is registered as an agent tool

## Phase 3: Charge and gate (PR 3) — US3

- [ ] T014 [PR3] Settings `CREDITS_PER_USD` (no shipped default), `MARKUP`, `CREDITS_ENFORCEMENT` (default off), `CREDIT_CHECK_FAILURE_POLICY`; `.env.example` entries
- [ ] T015 [PR3] Charge credits on the event (rate stored on the row); unpriced debits nothing and is counted
- [ ] T016 [PR3] New limit kind in `budgets.check_allowance`; `ErrorCode.INSUFFICIENT_CREDITS`; holds expressed in credits; refusal before any model work; `GET /usage` returns the balance
- [ ] T017 [PR3] Fail-policy tests (wallet unreadable under `open` and `closed`), and a test that enforcement off changes nothing (SC-005)

## Phase 4: Port, inbox, webhooks (PR 4) — US4

- [ ] T018 [PR4] `app/billing/providers/base.py` (the port), `fake.py` (signs and parses its own payloads), and `tests/billing/contract.py` parameterised over registered adapters
- [ ] T019 [PR4] `postgres-init/21-billing.sql`: `billing_customers`, `credit_products`, `billing_webhook_events`
- [ ] T020 [PR4] Failing tests first: duplicate and concurrent delivery (one grant); invalid signature (rejected, counted, nothing applied); unlinked customer (quarantined, alerted); a payload naming a different tenant (grant goes to the linked one); refund after spend (negative balance, usage refused)
- [ ] T021 [PR4] `POST /billing/webhooks/{provider}` with the seven steps in the contract; body-size cap before read; refuses to start when a configured provider has no secret
- [ ] T022 [PR4] Alert `BillingWebhookQuarantined`; scrub PII from the stored payload; retention for the inbox

## Phase 5: Export (PR 5) — US5

- [ ] T023 [PR5] `postgres-init/22-usage-export-outbox.sql`; rows created in the event's transaction, only for a tenant linked to a `USAGE_EXPORT` provider
- [ ] T024 [PR5] Failing tests: fail twice then succeed (one delivery); two workers (no double send, via `SKIP LOCKED` on a real Postgres); an event past 30 days is `expired`, counted and alerted
- [ ] T025 [PR5] `scripts/billing_export_worker.py` + `make billing-export-worker`: batches, exponential backoff with a cap, bounded attempts; alerts `UsageExportStuck` and `UsageExportExpired`

## Phase 6: Reconcile and operate (PR 6) — US6

- [ ] T026 [PR6] Reconciliation of events vs `usage_ledger` vs the gateway spend log (by `end_user`), and optionally a provider balance; a test that deletes one event and expects the report to name tenant, day and amount
- [ ] T027 [PR6] `scripts/credits.py` + `make credits`: `grant`, `adjust`, `show` (lots, entries, balance); `--by` and `--reason` required
- [ ] T028 [PR6] Runbook in `infra/README.md`; Grafana panel for balance, grant and debit rates, export lag

## Later (separate PRs, each needs a sandbox account)

- [ ] T029 One adapter per provider, written against primary docs (R5 O-C, O-D), passing the contract suite unchanged
- [ ] T030 Make `budgets` read `usage_events`; stop the per-turn `usage_ledger` write; delete the dual-write test

## Dependencies

PR 1a → 2 → 3. PR 2 → 4. PR 1a and 4 → 5. PR 2 and 5 → 6. Adapters need 4 and 5. Only PR 3 changes a customer-visible
behaviour, and only when `CREDITS_ENFORCEMENT` is turned on.
