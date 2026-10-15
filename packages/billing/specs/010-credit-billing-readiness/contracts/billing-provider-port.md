# Contract: the billing-provider port

A provider (Stripe, Polar, PayPal, or a metering product such as Orb or Lago) is one adapter module under `app/billing/providers/`
that implements this port. Everything else (the wallet, the webhook inbox, the export outbox, the catalog, gating) is provider-agnostic
and does not change when an adapter is added (SC-004). An adapter holds **no** business rules: it translates wire formats.

## The port

```python
class Capability(Enum):
    CHECKOUT = "checkout"          # can start a purchase of a credit pack
    USAGE_EXPORT = "usage_export"  # accepts per-call usage events (Stripe meters, Polar events)
    BALANCE_READ = "balance_read"  # can report its own balance, for reconciliation only

class BillingProvider(Protocol):
    name: str                                   # "stripe" | "polar" | "paypal" | "fake"; the {provider} path segment
    capabilities: frozenset[Capability]

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[BillingEvent]:
        """Verify the signature FIRST (and any replay window the provider defines), then normalize.
        Raises InvalidSignature on any failure. Pure: no I/O, no database."""

    async def create_checkout(self, customer: BillingCustomer, product: CreditProduct, *,
                              idempotency_key: str, success_url: str, cancel_url: str) -> CheckoutSession:
        """Only when CHECKOUT is declared. Sets the provider-side reference the webhook will echo."""

    async def export_usage(self, customer: BillingCustomer, events: Sequence[UsageEvent]) -> ExportResult:
        """Only when USAGE_EXPORT is declared. Uses event.event_id as the provider idempotency key."""

    async def read_balance(self, customer: BillingCustomer) -> Decimal:
        """Only when BALANCE_READ is declared. Used by reconciliation; never by gating."""
```

`ExportResult` carries `accepted`, `duplicate` (a provider that reports a skipped duplicate, as Polar does, counts as success) and
`failed` ids, each failure classed `retryable` or `permanent` so the outbox knows whether to back off or give up.

## Normalized webhook events

`BillingEvent(provider, event_id, kind, customer_ref, product_ref, payment_ref, amount_minor, currency, occurred_at, raw_type)`. **There is no `tenant` field, on purpose**: a payload that
names a tenant has nowhere to put it. `payment_ref` (added in PR 4) is the provider's payment or order id, the same on a purchase and the refund that reverses it: it is how a
refund finds the grant it takes back, and how a second event for an already-granted payment is recognised. A purchase or refund with none is quarantined.

| `kind` | Meaning | What the app does |
|---|---|---|
| `CREDITS_PURCHASED` | a one-time pack was paid | grant `catalog[product_ref].credits` (lot source `purchase`) |
| `SUBSCRIPTION_PERIOD_STARTED` | a cycle began and includes credits | grant the plan's credits for that period (lot source `subscription`) |
| `PAYMENT_REFUNDED` | the **whole** payment was returned | clawback what the original grant still holds or spent (policy D6/D12); at most once per payment |
| `PAYMENT_PARTIALLY_REFUNDED` | part of a payment was returned | **quarantine and alert** (D12: no pro-rata policy exists) |
| `DISPUTE_OPENED` / `DISPUTE_CLOSED` | a chargeback | **quarantine and alert** (D12: not decided; was "clawback and restore" in the first draft) |
| `IGNORED` | a type the app does not act on | record in the inbox, do nothing |

`amount_minor` is informational. **Credits are never computed from it** (FR-015). `event_id` is the provider's own event id and the inbox's dedupe key.

## The webhook endpoint

`POST /billing/webhooks/{provider}`. It is deliberately **not** behind the tenant-identity headers (a provider cannot send them), so its
authenticity is the signature alone, and it is built defensively:

1. Unknown `{provider}` → 404, counted. A provider that is configured but has no secret set → refuses to start (fail closed), like the Alertmanager receiver.
2. Body size capped (default 256 KiB) **before** it is read; rate-limited by source.
3. `parse_webhook` verifies the signature; `InvalidSignature` → 400, counted `invalid_signature`, **nothing stored as applied**.
4. Insert into the inbox `ON CONFLICT (provider, event_id) DO NOTHING`; a duplicate returns 200 immediately (the provider must stop retrying).
5. Resolve the tenant from `billing_customers` by `(provider, customer_ref)`. No link → `quarantined`, alerted, 200 (retrying cannot fix it).
6. Apply in **one transaction** with the status change (`received → applied`), using `idempotency_key = "{provider}:{event_id}"` for a grant and `clawback:{provider}:{payment_ref}` for a refund. A crash before commit leaves nothing applied for the retry.
7. Respond 2xx only after the commit. Any failure before that is a 5xx so the provider retries; a failure is recorded in its own transaction and the Nth quarantines the event (`BILLING_WEBHOOK_MAX_ATTEMPTS`). A refund whose purchase has not been applied yet is a 503 until `BILLING_REFUND_HOLD_HOURS` passes, then quarantined.

The route also carries a per-source rate limit (`BILLING_WEBHOOK_RATE_LIMIT_PER_MINUTE`, keyed on the client address, never on a header the sender controls). Behind a proxy every request
shares the proxy's address, so in practice it is one global ceiling (disclosed).

## Adapter contract tests

One suite, `tests/billing/contract.py`, parameterised over every registered adapter (the in-repo `fake` first, real ones later). An adapter that does not pass it cannot be registered.

- A valid signed payload yields the expected normalized events; a payload altered by one byte raises `InvalidSignature`.
- A replayed payload (same `event_id`) is applied once through the real inbox and wallet.
- Parsing never raises for an unknown event type (it yields `IGNORED`).
- If `USAGE_EXPORT`: re-sending the same `event_id` is reported as `duplicate` or succeeds, never as a second charge.
- If `CHECKOUT`: the same `idempotency_key` twice returns the same session, not two.
- No adapter reads the tenant from a payload field; the contract test supplies a payload naming a *different* tenant and asserts the grant goes to the linked one.
- The stored form of an event is a closed set of normalized fields; a payload full of buyer details (email, name, address, card) leaves none of them in the inbox.
- An authentic body in an unreadable shape is `InvalidPayload`, never `InvalidSignature` and never a crash.

**How it is enforced (PR 4).** The suite is `tests/billing/contract.py`, run by `tests/billing/test_provider_contract.py` (pure) and by the integration tier (the same harnesses through the real
inbox and wallet). An adapter is registered in `app/billing/providers/__init__.py` together with its `Harness`; `test_every_registered_adapter_has_a_harness` fails otherwise.

## Fit of the three target providers (what each adapter will have to do)

| | Checkout (sell a pack) | Webhooks | Usage export | Balance read |
|---|---|---|---|---|
| **Stripe** | Checkout Session for a one-time price | signed events, replay-tolerance window | Meter events: `identifier` = `event_id`, within 35 days | Credit Balance Summary |
| **Polar** | Checkout for a one-time product | Standard Webhooks signing (**Open O-D**) | `POST /v1/events/ingest`, `external_id` = `event_id` | Customer Meters |
| **PayPal** | Orders API order and capture (**Open O-C**) | signed webhooks (**Open O-C**) | none found: declare no `USAGE_EXPORT` | none found |

PayPal therefore runs in prepaid-wallet mode only, which the design supports because the wallet, not the provider, does the metering.
