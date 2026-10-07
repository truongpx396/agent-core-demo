"""Money in: one provider event becomes a grant, a clawback, or a visible refusal, exactly once
(specs/010-credit-billing-readiness, contracts/billing-provider-port.md steps 4-7; postgres-init/22-billing.sql).

A webhook is a statement by a stranger that a payment happened. It proves nothing the app hasn't checked: the
adapter has verified the signature before this module sees the event (`parse_webhook`), and this module then
decides everything that matters from tables the app wrote itself: WHO from `billing_customers`, HOW MUCH from
`credit_products`, and WHETHER IT IS NEW from the inbox. Nothing in the payload names a tenant or an amount that
counts (`BillingEvent` has no field for either).

## The duplicate story (constitution IV)

| Duplicate arrives as | Caught by |
|---|---|
| the same event delivered twice | inbox `PRIMARY KEY (provider, event_id)`: the second delivery finds a terminal row |
| the same event delivered twice AT ONCE | the same key: the second INSERT blocks on the first's transaction, then finds its row |
| a crash between "granted" and "marked applied" | impossible by construction: the grant and the status change are ONE transaction |
| the inbox row swept, then the event redelivered | the grant's own key `"{provider}:{event_id}"` in `credit_transactions`, kept for good |
| one payment described by two DIFFERENT events | the app checks for an existing grant of that `(tenant, provider, payment_ref)` and ignores the second; the unique index in 22 is the backstop for a race |
| a refund described by two different events | the clawback is keyed by the PAYMENT, `clawback:{provider}:{payment_ref}`, so a payment is taken back at most once |

## What can happen to an event, and what the caller is told

| Outcome | Inbox status | Provider is told | Counted |
|---|---|---|---|
| `applied` | `applied` | 2xx (stop retrying) | yes |
| `duplicate` | unchanged | 2xx | yes |
| `ignored` | `ignored` | 2xx | yes |
| `quarantined` | `quarantined` | 2xx: retrying cannot fix it (an unlinked customer stays unlinked) | yes, and ALERTED |
| `retry` | `received` | 5xx: a refund whose purchase has not arrived yet, bounded by `BILLING_REFUND_HOLD_HOURS` | yes |
| `failed` | `failed` | 5xx: it will be retried, `BILLING_WEBHOOK_MAX_ATTEMPTS` times, then quarantined | yes, and ALERTED |

`quarantined` is the outcome that hides money: the customer has paid and received nothing, and nobody is
retrying. It is therefore the one that pages (`BillingWebhookQuarantined`). Everything the app has not decided
how to handle goes there rather than being guessed at: a chargeback (what a dispute is worth is a product
decision) and a partial refund (likewise).

## The one gap this leaves, disclosed

An event stuck in `received` or `failed` whose provider then STOPS redelivering is not noticed until someone
looks: nothing polls the inbox. `BILLING_WEBHOOK_MAX_ATTEMPTS` bounds the loop while the provider keeps
retrying; it cannot bound one that has given up. The reconciliation (specs/010 PR 6) is the detector.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.agent.sql_store import get_connection
from app.billing import credits, store
from app.billing.providers.base import BillingEvent, CreditProduct, EventKind
from app.core import metrics
from app.core.config import BILLING_REFUND_HOLD_HOURS, BILLING_WEBHOOK_MAX_ATTEMPTS

logger = logging.getLogger(__name__)

Outcome = Literal["applied", "duplicate", "ignored", "quarantined", "retry", "failed"]
Action = Literal["grant", "clawback", "ignore", "quarantine", "hold"]

_PAID = (EventKind.CREDITS_PURCHASED, EventKind.SUBSCRIPTION_PERIOD_STARTED)
_TERMINAL = ("applied", "ignored", "quarantined")  # a delivery that finds one of these is a duplicate


@dataclass(frozen=True)
class Plan:
    """What to do with an event, decided from facts only (see `decide`)."""

    action: Action
    reason: str | None = None


def decide(
    event: BillingEvent,
    *,
    tenant: str | None,
    product: CreditProduct | None,
    lot: store.GrantedLot | None,
    received_at: datetime,
    now: datetime,
    hold: timedelta | None = None,
) -> Plan:
    """The whole policy, as a pure function of the event and what the app already knows, so every refusal
    reason is testable without a database. `tenant` is the LINKED tenant (or None), `product` the catalog
    entry (or None), `lot` the grant this event's payment already produced (or None)."""
    hold = hold if hold is not None else timedelta(hours=BILLING_REFUND_HOLD_HOURS)
    kind = event.kind
    if kind is EventKind.IGNORED:
        return Plan("ignore", "unhandled_type")
    if kind in (EventKind.DISPUTE_OPENED, EventKind.DISPUTE_CLOSED):
        return Plan("quarantine", "dispute_needs_a_decision")
    if kind is EventKind.PAYMENT_PARTIALLY_REFUNDED:
        return Plan("quarantine", "partial_refund_needs_a_decision")
    if not event.customer_ref:
        return Plan("quarantine", "no_customer")
    if tenant is None:
        return Plan("quarantine", "unlinked_customer")
    if not event.payment_ref:
        return Plan("quarantine", "no_payment_ref")

    if kind in _PAID:
        if lot is not None:
            return Plan("ignore", "payment_already_granted")
        if product is None or not product.active:
            return Plan("quarantine", "unknown_product")
        if kind is EventKind.SUBSCRIPTION_PERIOD_STARTED and product.expires_after_days is None:
            return Plan("quarantine", "subscription_without_expiry")  # the wallet would refuse it (credits.EXPIRY_REQUIRED)
        return Plan("grant")

    # PAYMENT_REFUNDED
    if lot is None:
        # The purchase may simply not have been delivered yet: hold the refund (the provider retries) until it is,
        # but only for `hold`: a wait on a counterparty needs a deadline.
        return Plan("quarantine", "purchase_never_applied") if now - received_at > hold else Plan("hold", "purchase_not_applied_yet")
    if lot.reclaimable <= 0:
        return Plan("ignore", "nothing_to_reclaim")
    return Plan("clawback")


async def process_event(event: BillingEvent) -> Outcome:
    """Applies one verified event. Never raises: a failure is recorded and reported as `failed`, so the caller
    can answer 5xx and the provider retries. Counted by outcome (`agent_billing_webhook_total`)."""
    try:
        outcome = await _process(event)
    except Exception as exc:  # noqa: BLE001 - whatever went wrong must be recorded and retried, never lost; the failure is counted and alerted
        logger.warning(
            "billing_webhook_failed",
            extra={"provider": event.provider, "event_id": event.event_id, "error_class": type(exc).__name__},
        )
        outcome = await _record_failure(event, type(exc).__name__)
    metrics.agent_billing_webhook_total.labels(provider=event.provider, outcome=outcome).inc()
    return outcome


async def _process(event: BillingEvent) -> Outcome:
    async with get_connection() as conn:  # ONE transaction: the decision, the money and the status commit together
        claimed = await _claim(conn, event)
        if claimed is None:
            return "duplicate"
        received_at, now = claimed
        tenant = await store.tenant_for_customer(conn, event.provider, event.customer_ref) if event.customer_ref else None
        product = lot = None
        if tenant is not None:
            if event.kind in _PAID and event.product_ref:
                product = await store.get_product(conn, event.provider, event.product_ref)
            if event.payment_ref and (event.kind in _PAID or event.kind is EventKind.PAYMENT_REFUNDED):
                lot = await store.granted_lot(conn, tenant, event.provider, event.payment_ref)
        plan = decide(event, tenant=tenant, product=product, lot=lot, received_at=received_at, now=now)
        return await _apply(conn, event, tenant, product, lot, plan)


async def _claim(conn: AsyncConnection, event: BillingEvent) -> tuple[datetime, datetime] | None:
    """Takes the event's inbox row, or None if it is already finished. Returns (when first received, now), both
    from the database's own clock, so a hold deadline cannot be skewed by an application host's.

    A first delivery inserts. A redelivery finds the row and LOCKS it (`FOR UPDATE`), which is what serialises
    two concurrent copies: the second INSERT already blocked on the first's transaction (the unique key), and
    only sees the row once that transaction has committed."""
    cur = await conn.execute(
        "INSERT INTO billing_webhook_events (provider, event_id, event_type, status, payload) "
        "VALUES (%s, %s, %s, 'received', %s) ON CONFLICT (provider, event_id) DO NOTHING RETURNING received_at, now()",
        (event.provider, event.event_id, event.raw_type, Jsonb(event.stored())),
    )
    row = await cur.fetchone()
    if row is not None:
        return row[0], row[1]
    cur = await conn.execute(
        "SELECT status, received_at, now() FROM billing_webhook_events WHERE provider = %s AND event_id = %s FOR UPDATE",
        (event.provider, event.event_id),
    )
    existing = await cur.fetchone()
    if existing is None:
        raise RuntimeError("the inbox row vanished between the conflict and the read")  # retention swept it: the retry handles it
    if existing[0] in _TERMINAL:
        return None
    await conn.execute(
        "UPDATE billing_webhook_events SET attempts = attempts + 1, status = 'received', updated_at = now() "
        "WHERE provider = %s AND event_id = %s",
        (event.provider, event.event_id),
    )
    return existing[1], existing[2]


async def _apply(
    conn: AsyncConnection,
    event: BillingEvent,
    tenant: str | None,
    product: CreditProduct | None,
    lot: store.GrantedLot | None,
    plan: Plan,
) -> Outcome:
    if plan.action == "grant":
        assert tenant is not None and product is not None and event.payment_ref is not None  # decide() guarantees it
        expires_at = datetime.now(UTC) + timedelta(days=product.expires_after_days) if product.expires_after_days else None
        await credits.grant_in(
            conn,
            tenant,
            product.credits,
            source="subscription" if event.kind is EventKind.SUBSCRIPTION_PERIOD_STARTED else "purchase",
            idempotency_key=f"{event.provider}:{event.event_id}",
            actor=f"webhook:{event.provider}",
            reason=event.kind.value,
            expires_at=expires_at,
            provider=event.provider,
            external_ref=event.payment_ref,
        )
        await _finish(conn, event, "applied", tenant, None)
        return "applied"

    if plan.action == "clawback":
        assert tenant is not None and lot is not None and event.payment_ref is not None
        applied = await credits.clawback_in(
            conn,
            tenant,
            lot.reclaimable,
            idempotency_key=f"clawback:{event.provider}:{event.payment_ref}",
            actor=f"webhook:{event.provider}",
            reason=event.kind.value,
        )
        if applied.status == "duplicate":
            await _finish(conn, event, "ignored", tenant, "already_reversed")
            return "ignored"
        if applied.status != "applied":
            await _finish(conn, event, "quarantined", tenant, f"clawback_{applied.status}")
            return _quarantined(event, f"clawback_{applied.status}")
        if applied.shortfall > 0:
            logger.warning(
                "billing_refund_after_spend",
                extra={"provider": event.provider, "event_id": event.event_id, "tenant": tenant, "debt": str(applied.shortfall)},
            )
        await _finish(conn, event, "applied", tenant, None)
        return "applied"

    if plan.action == "ignore":
        await _finish(conn, event, "ignored", tenant, plan.reason)
        return "ignored"

    if plan.action == "hold":
        await _finish(conn, event, "received", tenant, plan.reason)
        return "retry"

    await _finish(conn, event, "quarantined", tenant, plan.reason)
    return _quarantined(event, plan.reason)


def _quarantined(event: BillingEvent, reason: str | None) -> Outcome:
    logger.warning(
        "billing_webhook_quarantined",
        extra={"provider": event.provider, "event_id": event.event_id, "kind": event.kind.value, "reason": reason},
    )
    return "quarantined"


async def _finish(conn: AsyncConnection, event: BillingEvent, status: str, tenant: str | None, reason: str | None) -> None:
    await conn.execute(
        "UPDATE billing_webhook_events SET status = %s, tenant = %s, error_class = %s, updated_at = now() "
        "WHERE provider = %s AND event_id = %s",
        (status, tenant, reason, event.provider, event.event_id),
    )


async def _record_failure(event: BillingEvent, error_class: str) -> Outcome:
    """Records that applying `event` failed, in its OWN transaction (the failed one rolled back, and with it any
    trace of the event). The Nth failure quarantines it: a poison event must not be retried for ever. Itself
    best-effort and never raising: if even this write fails the outcome is still `failed`, the provider still
    retries, and the counter still pages."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO billing_webhook_events (provider, event_id, event_type, status, attempts, payload, error_class) "
                "VALUES (%s, %s, %s, 'failed', 1, %s, %s) ON CONFLICT (provider, event_id) DO UPDATE SET "
                "status = 'failed', attempts = billing_webhook_events.attempts + 1, error_class = EXCLUDED.error_class, updated_at = now() "
                "WHERE billing_webhook_events.status IN ('received', 'failed') RETURNING attempts",
                (event.provider, event.event_id, event.raw_type, Jsonb(event.stored()), error_class),
            )
            row = await cur.fetchone()
            if row is None:
                return "duplicate"  # it finished while this attempt was failing: nothing to record
            if row[0] >= BILLING_WEBHOOK_MAX_ATTEMPTS:
                await _finish(conn, event, "quarantined", None, f"max_attempts:{error_class}"[:100])
                return _quarantined(event, "max_attempts")
    except Exception as exc:  # noqa: BLE001 - recording a failure must not itself raise; the outcome is still "failed" and is counted and alerted
        logger.warning("billing_webhook_failure_unrecorded", extra={"error_class": type(exc).__name__})
    return "failed"
