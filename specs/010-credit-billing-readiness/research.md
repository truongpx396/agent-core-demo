# Research: Credit-Based Billing Readiness

Every claim is marked **Verified** (read from the provider's own documentation, or from this repository's code, on 2026-10-07),
**Search-only** (a search result, not a primary document: re-check before relying on it) or **Open** (an implementation PR must
verify it before building on it). The constitution (VII) forbids building on an unverified claim about third-party behaviour.

## R1. What the providers actually do

| Provider | Fact | Status | Source |
|---|---|---|---|
| Stripe | Meter event: `event_name` ≤100 chars, `payload.stripe_customer_id`, `payload.value`, optional `identifier` ≤100 chars, optional `timestamp` | Verified | docs.stripe.com/api/billing/meter-event/create |
| Stripe | `identifier` uniqueness is enforced "within a rolling period of at least 24 hours" and is meant for accidental retries, not as a long-term key | Verified | same |
| Stripe | `timestamp` must be within the **past 35 calendar days** or up to 5 minutes in the future | Verified | same |
| Stripe | A credit grant is "backed by an immutable, append-only ledger"; states pending, granted, depleted, expired, voided; optional `expires_at`, `priority` | Verified | docs.stripe.com/billing/subscriptions/usage-based/billing-credits |
| Stripe | Credits apply to an invoice **only when it finalizes**, and only to subscription items on **metered prices** reporting through Meters | Verified | same |
| Stripe | A customer may have at most **100 unused credit grants**; consumption order is priority, then earlier expiry, then promotional, then earlier effective date | Verified | same |
| Stripe | Credit Balance Summary distinguishes **ledger balance** from **available balance** (ledger less expired or unrecorded) | Verified | same |
| Polar | `POST /v1/events/ingest` takes a batch; each event needs `name` (≤128) and `customer_id` **or** `external_customer_id` | Verified | polar.sh/docs/api-reference/events/ingest |
| Polar | `external_id` deduplicates: duplicates are skipped and counted separately in the response | Verified | same |
| Polar | `metadata` ≤50 keys, key ≤40 chars, string value ≤500 chars; an `_llm` object (vendor, model, input/cached/output/total tokens) and `_cost` (amount in **cents**, currency USD only) | Verified | same |
| Polar | Credits are granted at the start of each subscription cycle, or once at a one-time purchase, and drawn from a meter balance | Verified | polar.sh/docs/features/usage-based-billing/credits |
| Polar | **"Polar doesn't block usage if the customer exceeds their balance. You're responsible for implementing the logic you need to prevent usage."** Balance is readable via Customer State or Customer Meters | Verified | same |
| PayPal | `PayPal-Request-Id` is an idempotency header, reportedly retained up to 45 days; a concurrent duplicate fails with a duplicate-key error | **Search-only** | developer.paypal.com/reference/guidelines/idempotency |
| PayPal | A usage-based billing product exists with subscription and wallet webhooks, but no first-party "meter event" ingestion was found | **Search-only** | developer.paypal.com/subscriptions/webhooks, docs.paypal.ai |

## R2. Design rules that follow

1. **The app wallet is the gate.** Stripe's grants settle on invoices and Polar does not block, so neither can answer "may this run now?". (FR-011)
2. **Ids must be deterministic and short.** A UUIDv5 string (36 chars) fits Stripe's 100 and Polar's `external_id`; it must not depend on time or a counter. (FR-001, FR-017)
3. **Export is an outbox with an age limit.** Stripe's 35-day window means a retry loop is only safe if it gives up loudly before 35 days; we use 30. (FR-017)
4. **Credits come from our catalog, never the payload.** A webhook proves a payment happened; what it is worth is our decision. (FR-015)
5. **Providers differ in shape, so the port has capabilities.** Stripe and Polar can take usage events; PayPal, as far as found, can only sell a credit pack (an order, then a webhook). Capabilities (`CHECKOUT`, `USAGE_EXPORT`, `BALANCE_READ`) let one wallet serve all three.
6. **Provider-held balances are mirrored, not trusted.** A reconciliation compares them to the wallet and reports drift (US6).

## R3. What the current meter does and does not do (Verified, from this repo's code)

| # | Finding | Evidence |
|---|---|---|
| G1 | **Follow-up suggestions and history compaction call the model and discard the usage.** Only the agent node adds to `total_cost_usd`, so this spend never reaches the ledger, the per-turn ceiling or any dollar cap. | `graph_followups.py` and `graph_compaction.py` read `response.content` only; `graph_agent_node.py:171` is the one accumulator |
| G2 | **Embeddings (retrieval queries and ingest) are not metered and, until now, were not attributed at the gateway.** `OpenAIEmbeddings.aembed_query` returns vectors only, so token usage is not visible app-side. | `app/retrieval/embeddings.py`: `embed_text`, `embed_texts` |
| G3 | **The ledger is one row per completed turn** with no unique key: nothing identifies a single model call, and a replayed write cannot be recognised as a duplicate. | `postgres-init/03-meter.sql`, `usage_ledger.record_usage` |
| G4 | **Cost is USD only.** There is no credit unit, no rate and no per-event price snapshot. | `usage_ledger`: `cost_usd NUMERIC(12,6)` |
| G5 | **The gateway already attributes chat calls to a tenant** (`end_user`, `tenant:<name>` tag), which makes an independent second meter possible for reconciliation. | `app/agent/gateway.py`, verified against a real LiteLLM in the cost-governance series |

G1 and G2 matter beyond billing: they are real spend invisible to today's dollar ceilings.

## R4. Alternatives considered

- **Let the provider be the wallet** (Stripe credit grants or Polar meters alone). Rejected: neither gates in real time (R1), and the app would be unable to refuse a request, which is the one thing a prepaid product must do.
- **Use LiteLLM's own budgets as the wallet** (per-end-user `max_budget`). Rejected as the wallet, kept as a backstop: it is a spend cap that resets, not a ledger of purchases, expiries and refunds, and it knows nothing about payments. The existing key backstop stays.
- **Buy a metering product** (Orb, Metronome, Lago, OpenMeter). Not rejected: the port is exactly the seam that lets one replace the outbox target. Building the wallet and the gate is still necessary, because gating must be in-process and synchronous.
- **Floating-point or integer credits.** Rejected: floats drift; integers overcharge small calls (a $0.0002 call would cost a whole credit at 1,000 credits per dollar).
- **Write events from the LiteLLM spend log only.** Rejected as the primary meter: it is asynchronous and gives no transactional tie to the debit. Kept as the independent second meter for reconciliation.

## R5. Open items (implementation PRs must verify before building on them)

- **O-A. RESOLVED 2026-10-07** (langchain-core 0.3.86, langchain-openai 0.3.35, langgraph 0.2.76), by reading the installed source and by a real run through `ChatOpenAI` into a mock gateway and a `MemorySaver` graph:
  - `AIMessage.id` is **assigned by langchain-core** (`_LC_ID_PREFIX` + the model run's id + the generation index), shaped `run--<uuid>-0`, **not taken from the provider's response**. It is unique per invocation: two identical calls that returned the *same* provider id still got different message ids.
  - It is **identical after a checkpoint round-trip** (`aget_state` returned the same id the node returned), and `usage_metadata` is present on the same message.
  - The provider's own id is present at `response_metadata["id"]` (`chatcmpl-…`), but it is **not** the event identity: a backend may return a fixed or missing one, and uniqueness must not depend on it.
  - **Decision:** `event_id = uuid5(NAMESPACE, f"{tenant}|{ai_message.id}")`, read from the response the instant `ainvoke` returns. A LangGraph node retry that re-calls the model gets a new id and a new event, which is correct: a second paid call happened. The streaming path assigns `run-<run_id>` (no index) by the same mechanism, so it is unique too. PR 1a adds a test that pins all of this, so a langchain upgrade that changes id assignment fails loudly instead of silently double-counting.
  - **Gap this does not close:** a call whose response arrives and whose process dies before the event commits is not recorded (the residual window in `plan.md`).
- **O-B.** Whether LiteLLM can return per-request usage for an embedding call through the OpenAI client path the app uses, or only via its spend log. (PR 1)
- **O-C.** PayPal's real shape for a credit-pack purchase (Orders capture, then webhook) and its webhook signature verification, from primary docs. (adapter PR, not this feature)
- **O-D.** Stripe webhook signature scheme and tolerance, Polar's webhook signing (Standard Webhooks), from primary docs. (adapter PRs)
