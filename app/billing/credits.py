"""The credit wallet: grants, debits and expiries as an append-only ledger (postgres-init/20).

A tenant is on credit billing if and only if it has a `credit_accounts` row; for any other tenant
every function here is a no-op that reports `no_account`, so adding the wallet changes nothing for an
existing tenant until an operator, or a verified purchase webhook, opens an account.

## The guarantees, and what gives each one

  * **Exactly once.** Every grant, debit and expiry carries an idempotency key, and
    `UNIQUE (tenant, idempotency_key)` on `credit_transactions` makes a replay insert nothing. A debit
    is keyed by the usage event that caused it, so a replayed model call cannot be charged twice.
  * **A serial outcome under concurrency.** Every write for one tenant first takes
    `pg_advisory_xact_lock(hash(tenant))`. The row locks on the lots already stop a lost update on a
    lot's `remaining`, and the tests show every other guarantee holds without the tenant lock. What
    only the lock gives is the CANONICAL state: a debit racing a grant could otherwise book a debt
    next to live credit (the net balance is the same, but a wallet that owes and holds at once is one
    nobody can explain), so grants and debits end exactly as a serial run would
    (tests: `TestTheCanonicalState`, which fails 3 of 3 times when the lock is removed). A per-tenant
    lock is simpler to reason about than ordering locks across lots, and a tenant's model-call rate is
    low enough that it is never the bottleneck; revisit only if a measured rate says otherwise. It is
    held to the end of the caller's transaction, which is the point: the debit commits (or not)
    together with whatever caused it.
  * **Credits are exact.** `NUMERIC(18,6)` and `Decimal`, never a float: floats drift, and an integer
    credit would overcharge every small call.
  * **The ledger is the truth.** `credit_entries` is append-only and a lot's `remaining` is a cache
    kept equal to the sum of its entries inside the lock; `verify()` reports any drift.
  * **A debit never fails because the balance is short.** The model call it pays for has already
    happened, so refusing to record it would only lose the fact. The shortfall is booked on the
    tenant's `overdraft` lot (a negative `remaining`), counted, and repaid first by the next grant.
    Stopping the spend is the job of gating (`budgets.check_allowance`, checked BEFORE the call), not of the debit.

## Consumption order

Earliest-expiring lot first (a lot with no expiry last), then oldest. A lot past its `expires_at` is
never consumed, whether or not the sweep (`expire_due`) has booked its expiry yet, so the available
balance is right the instant a lot expires.

The `*_in` functions run inside the caller's transaction (`sql_store.get_connection()` is one) so a
debit can commit with the usage event that caused it; the plain ones open their own.
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from psycopg import AsyncConnection

from app.agent.sql_store import get_connection
from app.core import metrics

logger = logging.getLogger(__name__)

ZERO = Decimal("0.000000")
_UNIT = Decimal("0.000001")
# Where credits can come from. 'overdraft' is the wallet's own bookkeeping, never granted.
GRANT_SOURCES = frozenset({"purchase", "subscription", "promo", "manual", "adjustment"})
# Credits that MUST carry an expiry (spec 010 decision D9). Promotional credits are a liability that
# should be capped in time; a subscription's credits belong to its period and do not roll over. Paid
# credits ('purchase') do not expire unless the caller says so: many jurisdictions restrict expiring
# a prepaid balance someone paid for, so that is left to the operator, never the default.
EXPIRY_REQUIRED = frozenset({"promo", "subscription"})

Status = Literal["applied", "duplicate", "no_account", "nothing"]


@dataclass(frozen=True)
class Pricing:
    """What a debit was priced from, stored on its transaction (see postgres-init/20)."""

    cost_usd: Decimal
    credits_per_usd: Decimal
    markup: Decimal


@dataclass(frozen=True)
class Applied:
    """What one wallet call did. `duplicate` means the idempotency key had already been used, so
    nothing changed and the original transaction is returned; `nothing` is a zero amount."""

    status: Status
    transaction_id: str | None = None
    amount: Decimal = ZERO
    shortfall: Decimal = ZERO  # debit only: the part booked as overdraft

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass(frozen=True)
class Balance:
    available: Decimal  # live lots plus the overdraft (<= 0): what may be consumed right now
    ledger: Decimal  # every lot, including expired ones not yet swept, plus the overdraft
    debt: Decimal  # what is owed to the wallet, as a positive number


def credits_amount(value) -> Decimal:
    """`value` as an exact six-place decimal, rounding half up. A float is converted through its
    shortest repr (as a person wrote it), not its binary value, so 0.0000005 rounds up to 0.000001
    instead of down because its binary form happens to sit just below the half."""
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if not amount.is_finite():
        raise ValueError(f"credits must be a finite number, got {value!r}")
    return amount.quantize(_UNIT, rounding=ROUND_HALF_UP)


def credits_for_cost(cost_usd, credits_per_usd, markup) -> Decimal:
    """`credits = round_half_up(cost_usd x credits_per_usd x markup, 6)` (spec 010 data model).

    The cost is used at FULL precision and only the result is rounded. Rounding the cost first (it is
    a fraction of a cent, often under a millionth of a dollar for a cheap model or an embedding)
    would charge a $0.0000004 call nothing at all: a systematic loss, not noise. A float cost is read
    through its shortest repr, so 0.0123 is 0.0123."""
    cost = cost_usd if isinstance(cost_usd, Decimal) else Decimal(str(cost_usd))
    if not cost.is_finite() or cost < 0:
        raise ValueError(f"a cost must be a finite, non-negative number, got {cost_usd!r}")
    return credits_amount(cost * Decimal(credits_per_usd) * Decimal(markup))


def plan_allocation(
    lots: Sequence[tuple[str, Decimal]], need: Decimal
) -> tuple[list[tuple[str, Decimal]], Decimal]:
    """Which lots to take from, and how much is left over. `lots` must already be in consumption
    order. Pure, so the edge cases (exact fit, spanning lots, a shortfall) are tested without a database."""
    takes: list[tuple[str, Decimal]] = []
    left = need
    for lot_id, remaining in lots:
        if left <= 0:
            break
        if remaining <= 0:
            continue
        take = min(remaining, left)
        takes.append((lot_id, take))
        left -= take
    return takes, max(left, ZERO)


async def _one(cur) -> tuple:
    """The row a statement is certain to return (an INSERT ... RETURNING, an aggregate)."""
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the statement was expected to return a row and returned none")
    return row


async def _lock(conn: AsyncConnection, tenant: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant,))


async def _begin_transaction(
    conn: AsyncConnection,
    tenant: str,
    kind: str,
    key: str,
    *,
    actor: str,
    reason: str | None,
    usage_event_id: str | None = None,
    pricing: Pricing | None = None,
) -> tuple[str | None, str | None]:
    """Inserts the idempotency row. Returns (new_id, None), or (None, existing_id) for a replay."""
    cur = await conn.execute(
        "INSERT INTO credit_transactions "
        "(tenant, kind, idempotency_key, usage_event_id, cost_usd, credits_per_usd, markup, reason, actor) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant, idempotency_key) DO NOTHING RETURNING id",
        (
            tenant, kind, key, usage_event_id,
            pricing.cost_usd if pricing else None,
            pricing.credits_per_usd if pricing else None,
            pricing.markup if pricing else None,
            reason, actor,
        ),
    )
    row = await cur.fetchone()
    if row is not None:
        return str(row[0]), None
    cur = await conn.execute(
        "SELECT id FROM credit_transactions WHERE tenant = %s AND idempotency_key = %s", (tenant, key)
    )
    (existing,) = await _one(cur)
    return None, str(existing)


async def _move(conn: AsyncConnection, tenant: str, transaction_id: str, lot_id: str, amount: Decimal) -> None:
    """One signed entry, and the matching change to the lot's cached `remaining`."""
    await conn.execute(
        "INSERT INTO credit_entries (transaction_id, tenant, lot_id, amount) VALUES (%s, %s, %s, %s)",
        (transaction_id, tenant, lot_id, amount),
    )
    await conn.execute(
        "UPDATE credit_lots SET remaining = remaining + %s WHERE id = %s AND tenant = %s",
        (amount, lot_id, tenant),
    )


async def _overdraft_lot(conn: AsyncConnection, tenant: str, *, create: bool) -> tuple[str, Decimal] | None:
    if create:
        await conn.execute(
            "INSERT INTO credit_lots (tenant, source, granted, remaining, created_by) "
            "VALUES (%s, 'overdraft', 0, 0, 'system') ON CONFLICT (tenant) WHERE source = 'overdraft' DO NOTHING",
            (tenant,),
        )
    cur = await conn.execute(
        "SELECT id, remaining FROM credit_lots WHERE tenant = %s AND source = 'overdraft' FOR UPDATE", (tenant,)
    )
    row = await cur.fetchone()
    return (str(row[0]), row[1]) if row else None


async def grant_in(
    conn: AsyncConnection,
    tenant: str,
    amount,
    *,
    source: str,
    idempotency_key: str,
    actor: str,
    reason: str | None = None,
    expires_at: datetime | None = None,
    provider: str | None = None,
    external_ref: str | None = None,
) -> Applied:
    """Adds a lot. Opens the tenant's account if it has none. Any debt is repaid from it first."""
    credits = credits_amount(amount)
    if not tenant:
        raise ValueError("a grant needs a tenant")
    if credits <= 0:
        raise ValueError(f"a grant must be above zero, got {credits}")
    if source not in GRANT_SOURCES:
        raise ValueError(f"unknown grant source {source!r}; one of {sorted(GRANT_SOURCES)}")
    if not idempotency_key or not actor:
        raise ValueError("a grant needs an idempotency key and an actor (who did this)")
    if expires_at is None and source in EXPIRY_REQUIRED:
        raise ValueError(f"{source} credits must expire: pass expires_at (see EXPIRY_REQUIRED)")
    if expires_at is not None and (expires_at.tzinfo is None or expires_at <= datetime.now(UTC)):
        raise ValueError("expires_at must be a timezone-aware time in the future")

    await _lock(conn, tenant)
    await conn.execute(
        "INSERT INTO credit_accounts (tenant, created_by) VALUES (%s, %s) ON CONFLICT (tenant) DO NOTHING",
        (tenant, actor),
    )
    transaction_id, existing = await _begin_transaction(conn, tenant, "grant", idempotency_key, actor=actor, reason=reason)
    if transaction_id is None:
        return Applied("duplicate", transaction_id=existing)

    cur = await conn.execute(
        "INSERT INTO credit_lots (tenant, source, provider, external_ref, granted, remaining, expires_at, created_by) "
        "VALUES (%s, %s, %s, %s, %s, 0, %s, %s) RETURNING id",
        (tenant, source, provider, external_ref, credits, expires_at, actor),
    )
    (new_lot,) = await _one(cur)
    lot_id = str(new_lot)
    await _move(conn, tenant, transaction_id, lot_id, credits)

    debt = await _overdraft_lot(conn, tenant, create=False)
    if debt is not None and debt[1] < 0:
        repay = min(credits, -debt[1])
        await _move(conn, tenant, transaction_id, debt[0], repay)
        await _move(conn, tenant, transaction_id, lot_id, -repay)
    return Applied("applied", transaction_id, credits)


async def debit_in(
    conn: AsyncConnection,
    tenant: str,
    amount,
    *,
    idempotency_key: str,
    usage_event_id: str | None = None,
    actor: str = "system",
    reason: str | None = None,
    kind: str = "debit",
    pricing: Pricing | None = None,
) -> Applied:
    """Consumes credits, earliest-expiring first. A no-op (`no_account`) for a tenant with no account.

    Never fails because the balance is short: the shortfall is booked as overdraft (see module doc)."""
    credits = credits_amount(amount)
    if credits < 0:
        raise ValueError(f"a debit cannot be negative, got {credits}")
    if not tenant or not idempotency_key:
        raise ValueError("a debit needs a tenant and an idempotency key")
    if credits == 0:
        return Applied("nothing")

    # The opt-in check comes BEFORE the lock: every model call of every tenant reaches this, and a
    # tenant that is not on credit billing must cost one indexed lookup, not a serialising lock. An
    # account opened a moment later does not matter: credit billing starts when the account exists,
    # and an account is never removed.
    cur = await conn.execute("SELECT 1 FROM credit_accounts WHERE tenant = %s", (tenant,))
    if await cur.fetchone() is None:
        return Applied("no_account")
    await _lock(conn, tenant)
    transaction_id, existing = await _begin_transaction(
        conn, tenant, kind, idempotency_key, actor=actor, reason=reason, usage_event_id=usage_event_id, pricing=pricing
    )
    if transaction_id is None:
        return Applied("duplicate", transaction_id=existing)

    cur = await conn.execute(
        "SELECT id, remaining FROM credit_lots "
        "WHERE tenant = %s AND source <> 'overdraft' AND remaining > 0 AND (expires_at IS NULL OR expires_at > now()) "
        "ORDER BY expires_at NULLS LAST, created_at, id FOR UPDATE",
        (tenant,),
    )
    lots = [(str(lot_id), remaining) for lot_id, remaining in await cur.fetchall()]
    takes, shortfall = plan_allocation(lots, credits)
    for lot_id, take in takes:
        await _move(conn, tenant, transaction_id, lot_id, -take)
    if shortfall > 0:
        debt = await _overdraft_lot(conn, tenant, create=True)
        assert debt is not None  # just created or already there
        await _move(conn, tenant, transaction_id, debt[0], -shortfall)
        metrics.agent_credit_overdraft_total.inc()
        logger.warning("credit_overdraft", extra={"shortfall": str(shortfall)})
    return Applied("applied", transaction_id, credits, shortfall)


async def _expire_lot_in(conn: AsyncConnection, tenant: str, lot_id: str) -> bool:
    await _lock(conn, tenant)
    cur = await conn.execute(
        "SELECT remaining FROM credit_lots WHERE id = %s AND tenant = %s AND source <> 'overdraft' "
        "AND expires_at <= now() AND remaining > 0 FOR UPDATE",
        (lot_id, tenant),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    transaction_id, _ = await _begin_transaction(
        conn, tenant, "expire", f"expire:{lot_id}", actor="system", reason="lot reached its expiry"
    )
    if transaction_id is None:
        return False
    await _move(conn, tenant, transaction_id, lot_id, -row[0])
    return True


async def expire_due(*, limit: int = 100, tenant: str | None = None) -> int:
    """Books the expiry of lots past their `expires_at` that still hold credits. At most `limit` per
    call (a bounded batch: a cron run, not an unbounded drain), each in its own transaction so one
    failure cannot hold the rest. `tenant` narrows it to one tenant's lots. Returns how many were
    expired. Idempotent."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, tenant FROM credit_lots WHERE source <> 'overdraft' AND expires_at <= now() AND remaining > 0 "
            "AND (%(tenant)s::text IS NULL OR tenant = %(tenant)s) ORDER BY expires_at LIMIT %(limit)s",
            {"tenant": tenant, "limit": limit},
        )
        due = [(str(lot_id), tenant) for lot_id, tenant in await cur.fetchall()]
    expired = 0
    for lot_id, lot_tenant in due:
        async with get_connection() as conn:
            expired += await _expire_lot_in(conn, lot_tenant, lot_id)
    return expired


async def _read_balance(conn: AsyncConnection, tenant: str) -> tuple[bool, Balance]:
    """(has an account, balance) in ONE statement. The join starts from `credit_accounts`, so a tenant
    with an account and no lots yet is one row (zero credits) while a tenant with no account is no row
    (`COUNT` of 0): the gate must tell "not on credit billing" apart from "on it, with nothing left"."""
    cur = await conn.execute(
        "SELECT COUNT(DISTINCT a.tenant), "
        "COALESCE(SUM(l.remaining) FILTER (WHERE l.source <> 'overdraft' AND (l.expires_at IS NULL OR l.expires_at > now())), 0), "
        "COALESCE(SUM(l.remaining), 0), "
        "COALESCE(-SUM(l.remaining) FILTER (WHERE l.source = 'overdraft'), 0) "
        "FROM credit_accounts a LEFT JOIN credit_lots l ON l.tenant = a.tenant WHERE a.tenant = %s",
        (tenant,),
    )
    accounts, live, ledger, debt = await _one(cur)
    return accounts > 0, Balance(available=live - debt, ledger=ledger, debt=debt)


async def balance_in(conn: AsyncConnection, tenant: str) -> Balance:
    """The tenant's balance; all zeros for a tenant with no account (use `account_balance_in` to tell)."""
    return (await _read_balance(conn, tenant))[1]


async def account_balance_in(conn: AsyncConnection, tenant: str) -> Balance | None:
    """The balance of a tenant that is ON credit billing, or None for one that is not (no account:
    never debited, never gated, spec D8). One round trip, which matters because the gate reads it
    before every turn of an enforcing deployment."""
    has_account, balance = await _read_balance(conn, tenant)
    return balance if has_account else None


async def verify_in(conn: AsyncConnection, tenant: str) -> list[str]:
    """Every lot whose cached `remaining` disagrees with the sum of its entries (the truth), as
    human-readable lines. Empty means the wallet is consistent."""
    cur = await conn.execute(
        "SELECT l.id, l.remaining, COALESCE(SUM(e.amount), 0) FROM credit_lots l "
        "LEFT JOIN credit_entries e ON e.lot_id = l.id WHERE l.tenant = %s "
        "GROUP BY l.id, l.remaining HAVING l.remaining <> COALESCE(SUM(e.amount), 0)",
        (tenant,),
    )
    return [f"lot {lot_id}: remaining {remaining} but its entries sum to {total}" for lot_id, remaining, total in await cur.fetchall()]


async def grant(tenant: str, amount, **kwargs) -> Applied:
    async with get_connection() as conn:
        return await grant_in(conn, tenant, amount, **kwargs)


async def debit(tenant: str, amount, **kwargs) -> Applied:
    async with get_connection() as conn:
        return await debit_in(conn, tenant, amount, **kwargs)


async def balance(tenant: str) -> Balance:
    async with get_connection() as conn:
        return await balance_in(conn, tenant)


async def account_balance(tenant: str) -> Balance | None:
    async with get_connection() as conn:
        return await account_balance_in(conn, tenant)


async def verify(tenant: str) -> list[str]:
    async with get_connection() as conn:
        return await verify_in(conn, tenant)
