"""Recording billable usage: rate it, keep it, charge it, queue it for export, in the caller's transaction (specs/013-billing-owns-the-meter).

A host application meters its own usage in whatever shape suits it (the agent app: one row per model call, read by its spend caps).
Billing needs a record it can rate, charge, export and reconcile without knowing that shape, so the host hands the package one plain
value per unit of usage and the package does the rest. This module is that one call, and it is everything a second project needs to
write instead of writing a meter's billing half:

```python
await billing.record_usage_in(conn, Usage(tenant="acme", event_id="call-123", kind="chat", cost_usd=0.0123, model="gpt", tokens=150))
```

## What one call does, in order, all on the caller's connection

  1. **Nothing, if nothing is billing.** With no `CREDITS_PER_USD` and no enabled provider that bills on usage there is no use for the
     record, so no statement is issued (`Recorded("off")`). A deployment that has not set billing up pays for one list lookup.
  2. **Rates** the cost: `credits = round_half_up(cost_usd x CREDITS_PER_USD x MARKUP, 6)`. The cost is first rounded to the column's
     twelve places and THAT figure is what is stored and multiplied, so the row reproduces its own credits exactly
     (`credits == credits_for_cost(cost_usd, credits_per_usd, markup)` for every stored row). An unpriced call (`cost_usd=None`) has
     NULL credits and debits nothing: unknown is never silently free.
  3. **Records** the usage in `billing_usage_events` (24) with `ON CONFLICT (tenant, event_id) DO NOTHING`. A repeat of the same
     `(tenant, event_id)` stops here: it inserts, debits and queues nothing (`Recorded("duplicate")`), which is what makes a replayed
     call safe. The tenant is part of the key, so two tenants may use the same id.
  4. **Debits** the tenant's wallet for the credits, keyed by the event id (`credits.debit_in`). A tenant with no wallet costs one indexed
     lookup and nothing else. The debit's own idempotency key is the same event id, so the two layers agree.
  5. **Queues** the event for every enabled provider that bills on usage and that the tenant is linked to, in `billing_export_outbox`
     (25), so the event and its pending export cannot disagree about what exists.

## It never raises for an operational fault

The caller has already recorded its own usage (the agent's `usage_events` row is what its caps sum), so a fault here must not take that
record, or the turn it belongs to, down with it. But a billing record that is not written is lost revenue that nobody is told about by a
log line, so every way it can fail is counted and alerted:

  * the whole body runs in one savepoint; a fault in the record itself (the table is missing, the database is unreachable) undoes
    only what this call wrote and is counted as `agent_cost_governance_degraded_total{path="billing_event_write"}` -> BillingEventWriteFailing;
  * **the debit runs in its own savepoint**: a wallet fault undoes only the debit, and the event is kept WITH its credits. The missing charge
    is visible (`credit-billing-reconcile` lists it) and replayable: the debit's key IS the event id and the row holds every figure it needs.
    Counted as `{path="credit_debit"}` -> CreditDebitFailing. The alternative, one all-or-nothing body, would turn a wallet outage into a
    loss of the record, and a lost record cannot be repaired while an uncharged one can;
  * **queuing runs in its own savepoint** for the same reason: counted as `{path="export_enqueue"}` -> UsageExportEnqueueFailing.

Invalid input (a blank tenant, id or kind, a negative or non-finite cost) is not raised either, for the same reason: it is counted as
`billing_event_write` and logged by field name (never by value). A caller that wants to fail loudly on a programming error validates
before calling.

## Contract a caller must honour

  * `event_id` identifies the unit of usage within its tenant and MUST be stable across retries of the same unit (the agent derives it
    from the model response's own id), and unique across the tenants of one instance if a usage-billing provider is enabled: the provider
    deduplicates on the id alone.
  * The call must be made on the connection and in the transaction that records the caller's own event, so the two commit or roll back
    together and a replay that finds the caller's event already there never reaches this function. "After the caller's own statement" is the
    point: psycopg opens a transaction at a connection's first statement, so this function's savepoints then nest inside it. Called on a
    connection with NO open transaction, its outermost block is the transaction and commits when it exits, which is right for a caller that
    has nothing else to keep atomic and wrong for one that thought it did.
  * `cost_usd` is the caller's figure: only the caller knows its model prices.

Imports nothing from `app` (enforced by tests/test_boundary.py).
"""
import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from credit_billing import credits, metrics, providers
from credit_billing.credits import Applied, Pricing
from credit_billing.settings import BILLING_PROVIDERS, CREDITS_PER_USD, MARKUP

logger = logging.getLogger(__name__)

# `cost_usd` has twelve decimal places in the table; the credits are computed from the cost AS STORED.
_COST_UNIT = Decimal("0.000000000001")

Status = Literal["recorded", "duplicate", "off", "failed"]

_warned_missing_table = False


@dataclass(frozen=True)
class Usage:
    """One unit of billable usage, as the caller knows it."""

    tenant: str
    event_id: str  # stable across retries of the same unit; unique within the tenant (and across tenants if a provider is exported to)
    kind: str  # the caller's category; not a closed set here
    cost_usd: Decimal | float | int | str | None  # None: unpriced (unknown, never zero)
    model: str | None = None  # descriptive; sent to a usage-billing provider
    tokens: int | None = None  # idem


@dataclass(frozen=True)
class Recorded:
    """What one call did. `failed` means nothing was recorded and the fault was counted; `off` means nothing is billing."""

    status: Status
    credits: Decimal | None = None
    debit: Applied | None = None  # None when nothing was debited (unpriced, no wallet result yet, or the debit failed and was counted)


def _degrade(path: str) -> None:
    metrics.agent_cost_governance_degraded_total.labels(path=path).inc()


def _valid(usage: Usage) -> tuple[Decimal | None, Decimal | None]:
    """(the cost to store, the credits it is worth), or ValueError naming the FIELD that is wrong, never its value."""
    for name in ("tenant", "event_id", "kind"):
        value = getattr(usage, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-blank string")
    if usage.tokens is not None and (isinstance(usage.tokens, bool) or not isinstance(usage.tokens, int) or usage.tokens < 0):
        raise ValueError("tokens must be a non-negative integer")
    if usage.cost_usd is None:
        return None, None
    try:
        cost = Decimal(str(usage.cost_usd)).quantize(_COST_UNIT, rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError):  # str() of a float is its shortest repr (as a person wrote it); NaN and infinity do not quantize
        raise ValueError("cost_usd must be a finite number") from None
    if not cost.is_finite():
        raise ValueError("cost_usd must be a finite number")
    if cost < 0:
        raise ValueError("cost_usd must not be negative")
    if CREDITS_PER_USD is None:
        return cost, None
    return cost, credits.credits_for_cost(cost, CREDITS_PER_USD, MARKUP)


_INSERT = (
    "INSERT INTO billing_usage_events (tenant, event_id, kind, cost_usd, credits, credits_per_usd, markup, model, tokens) "
    "VALUES (%(tenant)s, %(event_id)s, %(kind)s, %(cost_usd)s, %(credits)s, %(credits_per_usd)s, %(markup)s, %(model)s, %(tokens)s) "
    "ON CONFLICT (tenant, event_id) DO NOTHING"
)

_ENQUEUE = (
    "INSERT INTO billing_export_outbox (provider, tenant, event_id) "
    "SELECT c.provider, %(tenant)s, %(event_id)s FROM billing_customers c "
    "WHERE c.tenant = %(tenant)s AND c.provider = ANY(%(providers)s) ON CONFLICT (provider, tenant, event_id) DO NOTHING"
)


async def record_usage_in(conn: AsyncConnection, usage: Usage) -> Recorded:
    """Rates, records, debits and queues `usage` on the caller's connection (see the module docstring). Never raises for a fault."""
    exporters = providers.usage_export_providers(BILLING_PROVIDERS)
    rate, markup = CREDITS_PER_USD, MARKUP  # read once: the row, the debit and the stored snapshot must agree
    if rate is None and not exporters:
        return Recorded("off")
    try:
        cost, worth = _valid(usage)
    except ValueError as exc:
        _degrade("billing_event_write")
        logger.warning("billing_usage_rejected", extra={"reason": str(exc)})
        return Recorded("failed")
    params = {
        "tenant": usage.tenant, "event_id": usage.event_id, "kind": usage.kind, "cost_usd": cost, "credits": worth,
        "credits_per_usd": rate, "markup": markup if rate is not None else None, "model": usage.model, "tokens": usage.tokens,
    }
    global _warned_missing_table
    try:
        async with conn.transaction():
            cur = await conn.execute(_INSERT, params)
            if cur.rowcount != 1:
                return Recorded("duplicate")
            debit = None
            if worth is not None and rate is not None and cost is not None:
                debit = await _charge(conn, usage, worth, Pricing(cost, rate, markup))
            if exporters:
                await _enqueue(conn, usage, exporters)
            return Recorded("recorded", worth, debit)
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn):
        _degrade("billing_event_write")
        if not _warned_missing_table:
            _warned_missing_table = True
            logger.warning(
                "billing_usage_events is missing; no usage is being recorded for billing until the package's schema is applied "
                "(credit-billing-migrate, or postgres-init/24-billing-usage-events.sql and 25-billing-export-outbox.sql)"
            )
    except Exception as exc:  # noqa: BLE001 - a billing fault must not fail the caller whose own record is already written; counted and alerted instead (BillingEventWriteFailing)
        _degrade("billing_event_write")
        logger.warning("billing_usage_write_failed", extra={"error_class": type(exc).__name__})
    return Recorded("failed")


async def _charge(conn: AsyncConnection, usage: Usage, worth: Decimal, pricing: Pricing) -> Applied | None:
    """Debits the tenant's wallet for the event just inserted, inside a savepoint so that a wallet fault undoes only the debit and never
    the event (module docstring). Never raises."""
    try:
        async with conn.transaction():
            return await credits.debit_in(
                conn, usage.tenant, worth, idempotency_key=usage.event_id, usage_event_id=usage.event_id,
                reason=f"usage ({usage.kind})", pricing=pricing,
            )
    except Exception as exc:  # noqa: BLE001 - a wallet fault must not lose the event it was charging for; counted and alerted instead (CreditDebitFailing)
        _degrade("credit_debit")
        logger.warning("credit_debit_failed", extra={"error_class": type(exc).__name__})
        return None


async def _enqueue(conn: AsyncConnection, usage: Usage, exporters: tuple[str, ...]) -> None:
    """Queues the event for every usage-billing provider in `exporters` this tenant is linked to, inside a savepoint so a fault undoes only
    the queuing. A tenant with no such link inserts nothing: the SELECT is over `billing_customers`, so the outbox only ever holds events
    someone has a customer to send them under. Never raises."""
    try:
        async with conn.transaction():
            await conn.execute(_ENQUEUE, {"event_id": usage.event_id, "tenant": usage.tenant, "providers": list(exporters)})
    except Exception as exc:  # noqa: BLE001 - a failure to queue must not lose the event it concerns; counted and alerted instead (UsageExportEnqueueFailing)
        _degrade("export_enqueue")
        logger.warning("usage_export_enqueue_failed", extra={"error_class": type(exc).__name__})


def reset_state() -> None:
    """Test hook: forget that the missing-table warning was already given."""
    global _warned_missing_table
    _warned_missing_table = False
