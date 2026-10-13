"""Reads and writes of the tables money-in stands on (postgres-init/22-billing.sql): the tenant link, the
product catalog, and the lookup of the grant a refund reverses.

Every function takes the caller's connection, so a webhook's whole decision (who, what, how much) is read and
acted on inside ONE transaction, and every statement is parameterised and fixed. These tables are written by
the app (checkout, an operator), never by a webhook: that is what makes them trustworthy answers to "which
tenant?" and "worth how much?" when the payload is untrusted.
"""
from dataclasses import dataclass
from decimal import Decimal

from psycopg import AsyncConnection

from app.billing.providers.base import CreditProduct


@dataclass(frozen=True)
class GrantedLot:
    """The grant a payment produced, and how much of it a refund can still take back."""

    lot_id: str
    granted: Decimal
    reclaimable: Decimal


async def link_customer(conn: AsyncConnection, tenant: str, provider: str, customer_ref: str) -> None:
    """Records that `customer_ref` is `tenant`'s customer at `provider`. Written by the app when it creates
    the provider-side customer at checkout, never from a webhook. Idempotent for the same link; a customer
    already linked to ANOTHER tenant raises (a unique violation) instead of being silently re-pointed."""
    await conn.execute(
        "INSERT INTO billing_customers (tenant, provider, customer_ref) VALUES (%s, %s, %s) "
        "ON CONFLICT (provider, customer_ref) DO UPDATE SET customer_ref = EXCLUDED.customer_ref WHERE billing_customers.tenant = EXCLUDED.tenant",
        (tenant, provider, customer_ref),
    )
    cur = await conn.execute(
        "SELECT tenant FROM billing_customers WHERE provider = %s AND customer_ref = %s", (provider, customer_ref)
    )
    row = await cur.fetchone()
    if row is None or row[0] != tenant:
        raise ValueError(f"{provider} customer {customer_ref!r} is already linked to a different tenant")


async def tenant_for_customer(conn: AsyncConnection, provider: str, customer_ref: str) -> str | None:
    """The ONLY way a webhook reaches a tenant. None means unlinked (the caller quarantines)."""
    cur = await conn.execute(
        "SELECT tenant FROM billing_customers WHERE provider = %s AND customer_ref = %s", (provider, customer_ref)
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def put_product(conn: AsyncConnection, product: CreditProduct) -> None:
    """Creates or updates a catalog entry. An operator action; a webhook never reaches it."""
    await conn.execute(
        "INSERT INTO credit_products (provider, product_ref, credits, expires_after_days, active) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (provider, product_ref) DO UPDATE SET "
        "credits = EXCLUDED.credits, expires_after_days = EXCLUDED.expires_after_days, active = EXCLUDED.active",
        (product.provider, product.product_ref, product.credits, product.expires_after_days, product.active),
    )


async def get_product(conn: AsyncConnection, provider: str, product_ref: str) -> CreditProduct | None:
    cur = await conn.execute(
        "SELECT credits, expires_after_days, active FROM credit_products WHERE provider = %s AND product_ref = %s",
        (provider, product_ref),
    )
    row = await cur.fetchone()
    return CreditProduct(provider, product_ref, row[0], row[1], row[2]) if row else None


async def granted_lot(conn: AsyncConnection, tenant: str, provider: str, payment_ref: str) -> GrantedLot | None:
    """The lot a payment's purchase or subscription grant created, for THIS tenant. None if that payment
    has not been applied (yet).

    `reclaimable` is what a refund may take back: everything granted EXCEPT credits that have already left
    the wallet by expiry (booked by the sweep, or past `expires_at` and not yet swept). Those are gone, so
    taking them back again would book a debt for credits the customer no longer has. Credits that were
    SPENT are reclaimable: that is the point of the refund policy (they become debt, spec D6)."""
    cur = await conn.execute(
        "SELECT l.id, l.granted, "
        "l.granted - COALESCE((SELECT -SUM(e.amount) FROM credit_entries e JOIN credit_transactions t ON t.id = e.transaction_id "
        "WHERE e.lot_id = l.id AND t.kind = 'expire'), 0) "
        "- CASE WHEN l.expires_at IS NOT NULL AND l.expires_at <= now() THEN l.remaining ELSE 0 END "
        "FROM credit_lots l WHERE l.tenant = %s AND l.provider = %s AND l.external_ref = %s "
        "AND l.source IN ('purchase', 'subscription') ORDER BY l.created_at LIMIT 1",
        (tenant, provider, payment_ref),
    )
    row = await cur.fetchone()
    return GrantedLot(str(row[0]), row[1], max(row[2], Decimal(0))) if row else None
