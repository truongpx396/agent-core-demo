"""The credit wallet: grants, debits and expiries as an append-only ledger (postgres-init/20).

A tenant is on credit billing if and only if it has a `credit_accounts` row; for any other tenant
every function here is a no-op that reports `no_account`, so adding the wallet changes nothing for an
existing tenant until an operator, or a verified purchase webhook, opens an account.

## The guarantees, and what gives each one

  * **Exactly once.** Every grant, debit and expiry carries an idempotency key, and
    `UNIQUE (tenant, idempotency_key)` on `credit_transactions` makes a replay insert nothing. A debit
    is keyed by the usage event that caused it, so a replayed model call cannot be charged twice.
  * **A key stands for ONE request** (spec 012). The transaction stores a normalised description of the
    request it was made for (`credit_transactions.request`), and a reused key is compared with it:
    the same request is a harmless `duplicate`, a DIFFERENT request raises `IdempotencyConflict` naming
    the fields that differ and changes nothing. Before this, a caller that asked for 600 credits under a key
    that had granted 500 was told "already applied" and its 600 was lost without a trace. Which fields identify
    a request is an explicit, validated choice per call (`compare=`); the default is every field. A row written
    before the column existed has no stored request and behaves exactly as it always did.
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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from credit_billing import metrics
from credit_billing.db import get_connection

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


# --- what a key stands for (spec 012, contracts/idempotency-request.md) ---------------------------------------------------------------
# The fields that define a request, per operation. A reused key is compared over these (the kind is always compared as well). They are
# named here, next to the writes they describe, so a caller that narrows the comparison does it by name and an unknown name is refused.
GRANT_FIELDS = ("amount", "source", "provider", "external_ref", "expires_at", "reason", "actor")
DEBIT_FIELDS = ("amount", "usage_event_id", "cost_usd", "credits_per_usd", "markup", "reason", "actor")
EXPIRE_FIELDS = ("lot_id",)


def _shown(value: str | None) -> str:
    if value is None:
        return "None"
    return repr(value[:80]) + ("…" if len(value) > 80 else "")


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a DIFFERENT request. Nothing was written. `differences` maps each differing field to
    (the value the key was first used with, the value now); the message truncates long values, the attribute does not.

    A `ValueError` so that callers which already turn a bad argument into an error (the operator command) keep working."""

    def __init__(self, key: str, tenant: str, differences: dict[str, tuple[str | None, str | None]]) -> None:
        self.key, self.tenant, self.differences = key, tenant, differences
        shown = "; ".join(f"{field}: {_shown(first)} -> {_shown(now)}" for field, (first, now) in differences.items())
        super().__init__(
            f"idempotency key {key!r} was already used for a different request ({shown}); nothing changed. "
            "A retry must send exactly the same request; a different request needs a new key."
        )


def _decimal(value) -> str | None:
    """A number as fixed-point text without an exponent or trailing zeros, so equal numbers are equal strings (0.0100 and 0.01 are
    both '0.01'; 100 is '100', not '1E+2'). It is the value the CALLER passed, not what Postgres stored after rounding it."""
    if value is None:
        return None
    return format((value if isinstance(value, Decimal) else Decimal(str(value))).normalize(), "f")


def _instant(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat(timespec="microseconds")


def _grant_request(credits: Decimal, source: str, provider, external_ref, expires_at, reason, actor) -> dict[str, str | None]:
    return {
        "kind": "grant", "amount": format(credits, "f"), "source": source, "provider": provider, "external_ref": external_ref,
        "expires_at": _instant(expires_at), "reason": reason, "actor": actor,
    }


def _debit_request(kind: str, credits: Decimal, usage_event_id, pricing: "Pricing | None", reason, actor) -> dict[str, str | None]:
    return {
        "kind": kind, "amount": format(credits, "f"), "usage_event_id": usage_event_id,
        "cost_usd": _decimal(pricing.cost_usd) if pricing else None,
        "credits_per_usd": _decimal(pricing.credits_per_usd) if pricing else None,
        "markup": _decimal(pricing.markup) if pricing else None,
        "reason": reason, "actor": actor,
    }


def _compared(compare: Collection[str] | None, vocabulary: Sequence[str]) -> list[str]:
    """The fields a reused key is judged on, in a stable order, validated BEFORE anything is written. `None` is every field of the
    operation. The kind is always compared, so naming it is allowed and changes nothing; any other unknown name is a bug at the call
    site, refused here rather than silently comparing nothing."""
    if compare is None:
        return ["kind", *vocabulary]
    unknown = sorted(set(compare) - set(vocabulary) - {"kind"})
    if unknown:
        raise ValueError(f"cannot compare {unknown}: the fields of this request are {list(vocabulary)}")
    return ["kind", *(field for field in vocabulary if field in set(compare))]


def _differences(stored: Mapping, new: Mapping, fields: Sequence[str]) -> dict[str, tuple[str | None, str | None]]:
    """The compared fields on which the stored request and the new one differ. A field the stored request does not contain (a request
    recorded by an older version with fewer keys) is skipped, so adding a field later never makes an old row conflict."""
    return {field: (stored[field], new.get(field)) for field in fields if field in stored and stored[field] != new.get(field)}


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
    request: Mapping[str, str | None],
    fields: Sequence[str],
    usage_event_id: str | None = None,
    pricing: Pricing | None = None,
) -> tuple[str | None, str | None]:
    """Inserts the idempotency row, with the request the key stands for. Returns (new_id, None), or (None, existing_id) for a replay of
    the SAME request. Raises IdempotencyConflict, having written nothing, for a replay of a DIFFERENT one.

    The comparison reads the row by `(tenant, key)`: a conflict is only ever found in the caller's own tenant and can never quote
    another tenant's request (constitution I). It compares the caller's values (`request`), not the stored columns, because the pricing
    columns round on storage and an exact compare of those would raise a conflict on a request that is identical. A stored request of
    NULL is a row written before the column existed: its original request is unknowable (the table is append-only, so it cannot be
    backfilled), and it keeps the behaviour it always had, a plain duplicate."""
    cur = await conn.execute(
        "INSERT INTO credit_transactions "
        "(tenant, kind, idempotency_key, usage_event_id, cost_usd, credits_per_usd, markup, reason, actor, request) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant, idempotency_key) DO NOTHING RETURNING id",
        (
            tenant, kind, key, usage_event_id,
            pricing.cost_usd if pricing else None,
            pricing.credits_per_usd if pricing else None,
            pricing.markup if pricing else None,
            reason, actor, Jsonb(dict(request)),
        ),
    )
    row = await cur.fetchone()
    if row is not None:
        return str(row[0]), None
    cur = await conn.execute(
        "SELECT id, request FROM credit_transactions WHERE tenant = %s AND idempotency_key = %s", (tenant, key)
    )
    existing, stored = await _one(cur)
    if stored is not None:
        differences = _differences(stored, request, fields)
        if differences:
            logger.warning("credit_idempotency_conflict", extra={"tenant": tenant, "key": key, "fields": sorted(differences)})
            raise IdempotencyConflict(key, tenant, differences)
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
    compare: Collection[str] | None = None,
) -> Applied:
    """Adds a lot. Opens the tenant's account if it has none. Any debt is repaid from it first.

    A reused `idempotency_key` is judged on `compare` (names from GRANT_FIELDS; default all; the kind always): the same request is a
    `duplicate`, a different one raises IdempotencyConflict. Narrow it only when the key names a fact and the other fields are
    incidental, and say why at the call site (the webhook grant leaves out `expires_at`, which it computes as now + days)."""
    fields = _compared(compare, GRANT_FIELDS)
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
    transaction_id, existing = await _begin_transaction(
        conn, tenant, "grant", idempotency_key, actor=actor, reason=reason, fields=fields,
        request=_grant_request(credits, source, provider, external_ref, expires_at, reason, actor),
    )
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
    metrics.agent_credit_granted_total.labels(source=source).inc(float(credits))
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
    compare: Collection[str] | None = None,
) -> Applied:
    """Consumes credits, earliest-expiring first. A no-op (`no_account`) for a tenant with no account.

    Never fails because the balance is short: the shortfall is booked as overdraft (see module doc). A reused `idempotency_key` is
    judged on `compare` (names from DEBIT_FIELDS; default all; the kind always), as in `grant_in`."""
    fields = _compared(compare, DEBIT_FIELDS)
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
        conn, tenant, kind, idempotency_key, actor=actor, reason=reason, usage_event_id=usage_event_id, pricing=pricing,
        fields=fields, request=_debit_request(kind, credits, usage_event_id, pricing, reason, actor),
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
        if kind == "debit":
            # Usage outrunning the wallet is the degradation this counter exists to show. A clawback that
            # outruns it is not: a refund after the credits were spent is the policy working (spec D6).
            metrics.agent_credit_overdraft_total.inc()
        logger.warning("credit_overdraft", extra={"shortfall": str(shortfall), "kind": kind})
    metrics.agent_credit_debited_total.labels(kind=kind).inc(float(credits))
    return Applied("applied", transaction_id, credits, shortfall)


async def clawback_in(
    conn: AsyncConnection,
    tenant: str,
    amount,
    *,
    idempotency_key: str,
    actor: str,
    reason: str | None = None,
    compare: Collection[str] | None = None,
) -> Applied:
    """Takes back credits because the money behind them was returned (a refund, spec D6). The same path
    as a debit (earliest-expiring lot first), recorded as kind 'clawback', and like a debit it is NEVER
    refused for a short balance: credits already spent become debt on the overdraft lot, which refuses new
    usage at the gate until the next grant repays it. A no-op (`no_account`) for a tenant with no wallet."""
    return await debit_in(
        conn, tenant, amount, idempotency_key=idempotency_key, actor=actor, reason=reason, kind="clawback", compare=compare
    )


async def adjust_in(
    conn: AsyncConnection,
    tenant: str,
    amount,
    *,
    idempotency_key: str,
    actor: str,
    reason: str,
    expires_at: datetime | None = None,
    compare: Collection[str] | None = None,
) -> Applied:
    """A correction made by hand (`src/credit_billing/cli/credits.py adjust`), signed: positive adds credits, negative takes them back.

    A positive adjustment is a grant whose lot source is 'adjustment' (so it repays debt first and can be told apart from
    a purchase or a promo in the lot list); a negative one goes the debit path as kind 'adjust' (earliest-expiring first,
    never refused for a short balance: the same rule as a clawback, because an operator correcting a mistake must not be
    blocked by the very mistake). Unlike a grant, the REASON is required: it is the one entry nobody else can explain.
    A negative adjustment on a tenant with no wallet is a no-op (`no_account`), never an account opened to hold a debt."""
    credits = credits_amount(amount)
    if credits == 0:
        raise ValueError("an adjustment of zero changes nothing")
    if not (reason or "").strip():
        raise ValueError("an adjustment needs a reason: it is a correction made by hand")
    if credits > 0:
        return await grant_in(
            conn, tenant, credits, source="adjustment", idempotency_key=idempotency_key, actor=actor, reason=reason,
            expires_at=expires_at, compare=compare,
        )
    if expires_at is not None:
        raise ValueError("a negative adjustment takes credits back; an expiry only applies to credits added")
    return await debit_in(
        conn, tenant, -credits, idempotency_key=idempotency_key, actor=actor, reason=reason, kind="adjust", compare=compare
    )


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
        conn, tenant, "expire", f"expire:{lot_id}", actor="system", reason="lot reached its expiry",
        fields=["kind", *EXPIRE_FIELDS], request={"kind": "expire", "lot_id": lot_id},
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


async def adjust(tenant: str, amount, **kwargs) -> Applied:
    async with get_connection() as conn:
        return await adjust_in(conn, tenant, amount, **kwargs)


async def balance(tenant: str) -> Balance:
    async with get_connection() as conn:
        return await balance_in(conn, tenant)


async def account_balance(tenant: str) -> Balance | None:
    async with get_connection() as conn:
        return await account_balance_in(conn, tenant)


async def verify(tenant: str) -> list[str]:
    async with get_connection() as conn:
        return await verify_in(conn, tenant)
