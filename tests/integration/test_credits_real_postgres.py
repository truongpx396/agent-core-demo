"""The credit wallet against a REAL Postgres (postgres-init/20-credit-wallet.sql, app/billing/credits.py).

A wallet is exactly the code a fake cursor cannot test: its guarantees ARE database behaviour.
`UNIQUE (tenant, idempotency_key)` is what makes a replayed debit harmless, an advisory lock is what
makes concurrent debits lose nothing, and the triggers and composite foreign keys are what stop a bug
(or a person with psql) rewriting the books. Each is stated and proven here (constitution VII:
reliance on a real constraint or lock is tested at this tier, not assumed). The arithmetic that
decides who is charged is in tests/billing/test_credit_planning.py.

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import asyncio
import random
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import errors as pg_errors

from app.billing import credits
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

D = Decimal


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:  # commits on a normal exit
            yield conn

    monkeypatch.setattr(credits, "get_connection", get_connection)
    return get_connection


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


async def grant(tenant, amount, key=None, **kwargs):
    kwargs.setdefault("source", "purchase")
    return await credits.grant(tenant, amount, idempotency_key=key or uuid.uuid4().hex, actor="test", **kwargs)


async def debit(tenant, amount, key=None, **kwargs):
    return await credits.debit(tenant, amount, idempotency_key=key or uuid.uuid4().hex, **kwargs)


async def scalar(url, sql, *params):
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]


async def entries_sum(url, tenant):
    return await scalar(url, "SELECT COALESCE(SUM(amount), 0) FROM credit_entries WHERE tenant = %s", tenant)


async def assert_consistent(url, tenant):
    """The wallet's own invariants: no drift between a lot's cache and its entries, the balance equals
    the entries, and no lot is negative except the overdraft lot."""
    assert await credits.verify(tenant) == []
    assert (await credits.balance(tenant)).ledger == await entries_sum(url, tenant)
    assert await scalar(url, "SELECT COUNT(*) FROM credit_lots WHERE tenant = %s AND source <> 'overdraft' AND remaining < 0", tenant) == 0


class TestGrantsAndTheAccount:
    async def test_a_grant_opens_the_account_and_a_replay_changes_nothing(self, appdata_url):
        tenant = tenant_name()

        first = await grant(tenant, 100, "buy-1")
        replay = await grant(tenant, 100, "buy-1")

        assert first.applied and replay.status == "duplicate"
        assert replay.transaction_id == first.transaction_id
        assert (await credits.balance(tenant)).available == D("100")
        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_lots WHERE tenant = %s", tenant) == 1
        await assert_consistent(appdata_url, tenant)

    async def test_the_same_grant_delivered_concurrently_is_one_grant(self, appdata_url):
        """A webhook the provider retries while the first attempt is still running."""
        tenant = tenant_name()

        results = await asyncio.gather(*[grant(tenant, 50, "buy-1") for _ in range(10)])

        assert [r.status for r in results].count("applied") == 1
        assert (await credits.balance(tenant)).available == D("50")
        await assert_consistent(appdata_url, tenant)

    async def test_a_debit_for_a_tenant_with_no_account_does_nothing_and_writes_nothing(self, appdata_url):
        tenant = tenant_name()

        result = await debit(tenant, 5)

        assert result.status == "no_account"
        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_transactions WHERE tenant = %s", tenant) == 0


class TestConsumptionOrder:
    async def test_the_sooner_expiring_lot_is_consumed_first_and_no_expiry_goes_last(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 10)  # never expires
        await grant(tenant, 10, expires_at=datetime.now(UTC) + timedelta(days=30))
        await grant(tenant, 10, expires_at=datetime.now(UTC) + timedelta(days=1))

        await debit(tenant, 10)  # exactly the soonest lot

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            cur = await conn.execute(
                "SELECT expires_at IS NULL, remaining FROM credit_lots WHERE tenant = %s ORDER BY expires_at NULLS LAST", (tenant,)
            )
            lots = await cur.fetchall()
        assert [remaining for _, remaining in lots] == [D("0"), D("10"), D("10")], "the day-old expiry was drained"
        await assert_consistent(appdata_url, tenant)

    async def test_a_debit_spans_lots_in_expiry_order(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 3, expires_at=datetime.now(UTC) + timedelta(days=1))
        await grant(tenant, 10)

        result = await debit(tenant, 5)

        assert result.shortfall == D("0")
        assert (await credits.balance(tenant)).available == D("8")
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            cur = await conn.execute(
                "SELECT remaining FROM credit_lots WHERE tenant = %s ORDER BY expires_at NULLS LAST", (tenant,)
            )
            assert [r for (r,) in await cur.fetchall()] == [D("0"), D("8")]
        await assert_consistent(appdata_url, tenant)

    async def test_money_is_exact_ten_debits_of_a_tenth_leave_exactly_nothing(self, appdata_url):
        """A float would leave 1.1e-16 of dust; NUMERIC leaves nothing."""
        tenant = tenant_name()
        await grant(tenant, 1)

        for _ in range(10):
            await debit(tenant, 0.1)

        assert (await credits.balance(tenant)).available == D("0")
        await assert_consistent(appdata_url, tenant)


class TestWhatADebitWasPricedFrom:
    async def test_the_cost_rate_and_markup_are_kept_on_the_transaction(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 100)
        pricing = credits.Pricing(cost_usd=D("0.0123"), credits_per_usd=D("1000"), markup=D("1.5"))

        result = await debit(tenant, credits.credits_for_cost(0.0123, 1000, D("1.5")), "event-1", pricing=pricing)

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            cur = await conn.execute(
                "SELECT cost_usd, credits_per_usd, markup FROM credit_transactions WHERE id = %s", (result.transaction_id,)
            )
            assert await cur.fetchone() == (D("0.012300000000"), D("1000.000000"), D("1.500000"))
        assert (await credits.balance(tenant)).available == D("100") - D("18.45")

    async def test_a_grant_carries_no_pricing(self, appdata_url):
        tenant = tenant_name()
        result = await grant(tenant, 5)

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            cur = await conn.execute("SELECT cost_usd, credits_per_usd, markup FROM credit_transactions WHERE id = %s", (result.transaction_id,))
            assert await cur.fetchone() == (None, None, None)


class TestATenantThatIsNotOnCreditBillingNeverWaitsOnTheLock:
    async def test_a_debit_for_it_returns_while_the_tenants_lock_is_held_elsewhere(self, appdata_url):
        """Every model call of every tenant reaches `debit_in`. A tenant with no account must cost one
        indexed lookup, not a serialising lock: shown by holding the lock from another connection."""
        free, paying = tenant_name(), tenant_name()
        await grant(paying, 10)
        async with await psycopg.AsyncConnection.connect(appdata_url) as holder:
            for tenant in (free, paying):
                await holder.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (tenant,))

            assert (await asyncio.wait_for(debit(free, 1), timeout=3)).status == "no_account"
            blocked = asyncio.ensure_future(debit(paying, 1))
            await asyncio.sleep(0.5)
            assert not blocked.done(), "a tenant ON credit billing still serialises on its lock"

            for tenant in (free, paying):
                await holder.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (tenant,))
            assert (await asyncio.wait_for(blocked, timeout=5)).applied


class TestOverdraft:
    async def test_a_debit_larger_than_the_balance_is_booked_not_refused(self, appdata_url):
        """The model call it pays for has already happened; refusing to record it would lose the fact."""
        tenant = tenant_name()
        await grant(tenant, 5)

        result = await debit(tenant, 8)

        assert result.applied and result.shortfall == D("3")
        balance = await credits.balance(tenant)
        assert (balance.available, balance.debt) == (D("-3"), D("3"))
        await assert_consistent(appdata_url, tenant)

    async def test_the_next_grant_repays_the_debt_first(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 5)
        await debit(tenant, 8)

        await grant(tenant, 10)

        balance = await credits.balance(tenant)
        assert (balance.available, balance.debt) == (D("7"), D("0"))
        await assert_consistent(appdata_url, tenant)

    async def test_a_grant_smaller_than_the_debt_only_reduces_it(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 1)
        await debit(tenant, 10)  # owes 9

        await grant(tenant, 4)

        balance = await credits.balance(tenant)
        assert (balance.available, balance.debt) == (D("-5"), D("5"))
        await assert_consistent(appdata_url, tenant)

    async def test_a_tenant_has_at_most_one_overdraft_lot(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 1)
        await debit(tenant, 2)
        await debit(tenant, 2)

        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_lots WHERE tenant = %s AND source = 'overdraft'", tenant) == 1
        assert (await credits.balance(tenant)).debt == D("3")


class TestReplayAndAtomicity:
    async def test_the_same_debit_key_is_applied_once(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 100)

        first = await debit(tenant, 10, "event-1")
        replay = await debit(tenant, 10, "event-1")

        assert first.applied and replay.status == "duplicate"
        assert (await credits.balance(tenant)).available == D("90")

    async def test_the_same_debit_delivered_concurrently_is_charged_once(self, appdata_url):
        """A replayed turn and a retried worker racing on the same usage event."""
        tenant = tenant_name()
        await grant(tenant, 100)

        results = await asyncio.gather(*[debit(tenant, 10, "event-1") for _ in range(20)])

        assert [r.status for r in results].count("applied") == 1
        assert (await credits.balance(tenant)).available == D("90")
        await assert_consistent(appdata_url, tenant)

    async def test_a_debit_commits_with_the_caller_or_not_at_all(self, appdata_url):
        """The point of `debit_in`: it runs in the caller's transaction, so it commits together with
        whatever caused it. If the caller fails after it, nothing is left, and the key is free again."""
        tenant = tenant_name()
        await grant(tenant, 100)

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            async with credits.get_connection() as conn:
                await credits.debit_in(conn, tenant, 10, idempotency_key="event-1")
                raise Boom

        assert (await credits.balance(tenant)).available == D("100")
        assert (await debit(tenant, 10, "event-1")).applied, "the rolled-back key is usable again"


class TestConcurrency:
    async def test_fifty_concurrent_debits_lose_no_update(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 1000)
        rng = random.Random(7)
        amounts = [D(str(round(rng.uniform(0.5, 9.5), 4))) for _ in range(50)]

        await asyncio.gather(*[debit(tenant, amount) for amount in amounts])

        assert (await credits.balance(tenant)).available == D("1000") - sum(amounts)
        await assert_consistent(appdata_url, tenant)

    async def test_concurrent_debits_beyond_the_balance_account_for_every_credit(self, appdata_url):
        """150 credits asked of a 100-credit wallet: 100 consumed, 50 booked as debt, none lost or invented."""
        tenant = tenant_name()
        await grant(tenant, 100)

        await asyncio.gather(*[debit(tenant, 3) for _ in range(50)])

        balance = await credits.balance(tenant)
        assert (balance.available, balance.debt) == (D("-50"), D("50"))
        assert await entries_sum(appdata_url, tenant) == D("-50")
        await assert_consistent(appdata_url, tenant)

    async def test_grants_and_debits_racing_still_balance(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 10)

        await asyncio.gather(
            *[debit(tenant, 1) for _ in range(30)], *[grant(tenant, 2) for _ in range(10)]
        )

        assert (await credits.balance(tenant)).available == D("10") + D("20") - D("30")
        await assert_consistent(appdata_url, tenant)


class TestTheCanonicalState:
    async def test_a_tenant_never_shows_debt_while_still_holding_live_credit(self, appdata_url):
        """What the per-tenant lock buys beyond row locks: grants and debits racing leave the wallet in
        the state a serial run would. A debit becomes debt only when no live credit remains, and a
        grant repays debt before it is spendable, so the books never show both at once (`available`
        would net out either way, but a debt next to credit is a wallet nobody can explain)."""
        for round_ in range(15):
            tenant = tenant_name()
            await grant(tenant, 1)  # opens the account
            await debit(tenant, 1)  # ...and empties it

            await asyncio.gather(
                *[debit(tenant, 2) for _ in range(12)], *[grant(tenant, 5) for _ in range(8)]
            )

            live = await scalar(
                appdata_url,
                "SELECT COALESCE(SUM(remaining), 0) FROM credit_lots WHERE tenant = %s AND source <> 'overdraft'",
                tenant,
            )
            debt = (await credits.balance(tenant)).debt
            assert not (live > 0 and debt > 0), f"round {round_}: live credit {live} next to a debt of {debt}"
            assert (await credits.balance(tenant)).available == D("40") - D("24")
            await assert_consistent(appdata_url, tenant)


class TestExpiry:
    EXPIRES_SOON = timedelta(seconds=1.5)

    async def test_an_expired_lot_is_never_consumed_even_before_the_sweep_books_it(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 10, expires_at=datetime.now(UTC) + self.EXPIRES_SOON)
        await asyncio.sleep(2)

        balance = await credits.balance(tenant)
        result = await debit(tenant, 4)

        assert balance.available == D("0"), "available is right the instant a lot expires"
        assert balance.ledger == D("10"), "the ledger still holds it until the expiry is booked"
        assert result.shortfall == D("4"), "an expired lot is not spent"
        await assert_consistent(appdata_url, tenant)

    async def test_the_sweep_books_the_expiry_once(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 10, expires_at=datetime.now(UTC) + self.EXPIRES_SOON)
        await asyncio.sleep(2)

        for _ in range(2):  # the second sweep finds nothing left to do for this lot
            await credits.expire_due(limit=1000, tenant=tenant)

        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_transactions WHERE tenant = %s AND kind = 'expire'", tenant) == 1
        assert (await credits.balance(tenant)).ledger == D("0")
        assert await scalar(appdata_url, "SELECT remaining FROM credit_lots WHERE tenant = %s AND source = 'purchase'", tenant) == D("0")
        await assert_consistent(appdata_url, tenant)

    async def test_the_sweep_is_bounded_by_its_limit_and_a_second_run_finishes_the_job(self, appdata_url):
        tenant = tenant_name()
        for _ in range(3):
            await grant(tenant, 1, expires_at=datetime.now(UTC) + self.EXPIRES_SOON)
        await asyncio.sleep(2)

        assert await credits.expire_due(limit=1, tenant=tenant) == 1
        assert await credits.expire_due(limit=10, tenant=tenant) == 2
        assert await credits.expire_due(limit=10, tenant=tenant) == 0
        await assert_consistent(appdata_url, tenant)


class TestTenantIsolation:
    async def test_one_tenants_debits_never_touch_anothers_lots(self, appdata_url):
        a, b = tenant_name(), tenant_name()
        await grant(a, 100)
        await grant(b, 100)

        await asyncio.gather(*[debit(a, 1) for _ in range(20)], *[debit(b, 2) for _ in range(20)])

        assert (await credits.balance(a)).available == D("80")
        assert (await credits.balance(b)).available == D("60")
        for tenant in (a, b):
            await assert_consistent(appdata_url, tenant)

    async def test_the_database_refuses_an_entry_that_crosses_tenants(self, appdata_url):
        """A bug that tried to move credits across tenants fails on the composite foreign keys,
        not on application code that might have the same bug."""
        a, b = tenant_name(), tenant_name()
        grant_a = await grant(a, 10)
        await grant(b, 10)
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            cur = await conn.execute("SELECT id FROM credit_lots WHERE tenant = %s", (a,))
            (lot_of_a,) = await cur.fetchone()
            with pytest.raises(pg_errors.ForeignKeyViolation):
                await conn.execute(
                    "INSERT INTO credit_entries (transaction_id, tenant, lot_id, amount) VALUES (%s, %s, %s, 1)",
                    (grant_a.transaction_id, b, lot_of_a),
                )


async def seeded_tenant() -> str:
    tenant = tenant_name()
    await grant(tenant, 10)
    await debit(tenant, 3)
    return tenant


class TestTheBooksCannotBeRewritten:
    async def _refused(self, url, sql, *params, match="append-only|only .remaining."):
        async with await psycopg.AsyncConnection.connect(url) as conn:
            with pytest.raises(pg_errors.RaiseException, match=match):
                await conn.execute(sql, params)

    async def test_entries_cannot_be_updated_or_deleted(self, appdata_url):
        tenant = await seeded_tenant()
        await self._refused(appdata_url, "UPDATE credit_entries SET amount = 99 WHERE tenant = %s", tenant)
        await self._refused(appdata_url, "DELETE FROM credit_entries WHERE tenant = %s", tenant)

    async def test_transactions_cannot_be_updated_or_deleted(self, appdata_url):
        tenant = await seeded_tenant()
        await self._refused(appdata_url, "UPDATE credit_transactions SET actor = 'x' WHERE tenant = %s", tenant)
        await self._refused(appdata_url, "DELETE FROM credit_transactions WHERE tenant = %s", tenant)

    async def test_a_lot_is_never_deleted_and_only_its_remaining_can_change(self, appdata_url):
        tenant = await seeded_tenant()
        await self._refused(appdata_url, "DELETE FROM credit_lots WHERE tenant = %s", tenant)
        await self._refused(appdata_url, "UPDATE credit_lots SET granted = 1000 WHERE tenant = %s", tenant)
        await self._refused(appdata_url, "UPDATE credit_lots SET expires_at = now() + interval '1 year' WHERE tenant = %s", tenant)
        await self._refused(appdata_url, "UPDATE credit_lots SET tenant = 'someone-else' WHERE tenant = %s", tenant)

    async def test_no_lot_but_the_overdraft_lot_can_go_negative(self, appdata_url):
        tenant = await seeded_tenant()
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation):
                await conn.execute("UPDATE credit_lots SET remaining = -1 WHERE tenant = %s", (tenant,))

    async def test_two_overdraft_lots_for_one_tenant_are_refused(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 1)
        await debit(tenant, 2)  # creates the overdraft lot

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.UniqueViolation):
                await conn.execute(
                    "INSERT INTO credit_lots (tenant, source, granted, remaining, created_by) VALUES (%s, 'overdraft', 0, 0, 'x')",
                    (tenant,),
                )

    async def test_an_empty_idempotency_key_is_refused_by_the_table_itself(self, appdata_url):
        tenant = tenant_name()
        await grant(tenant, 1)
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO credit_transactions (tenant, kind, idempotency_key, actor) VALUES (%s, 'debit', '', 'x')", (tenant,)
                )


class TestAgainstAnIndependentModel:
    async def test_a_long_random_run_matches_a_model_that_shares_no_code_with_the_database(self, appdata_url):
        """200 seeded grants and debits, with and without expiry and including debits far larger than the
        balance, compared after every step with a plain-Python model of the same rules. The model uses
        the pure planner for the arithmetic only; ordering, debt and repayment are written out again."""
        rng = random.Random(20261007)
        tenant = tenant_name()
        model_lots: list[dict] = []  # {"expires": datetime | None, "seq": int, "remaining": Decimal}
        model_debt = D("0")
        seq = 0
        await grant(tenant, 1, "seed")
        model_lots.append({"expires": None, "seq": seq, "remaining": D("1")})

        for step in range(200):
            seq += 1
            amount = D(str(round(rng.uniform(0.01, 25), 4)))
            if rng.random() < 0.35:
                expires = datetime.now(UTC) + timedelta(days=rng.choice([1, 7, 30])) if rng.random() < 0.5 else None
                await grant(tenant, amount, f"g{step}", expires_at=expires)
                repay = min(amount, model_debt)
                model_debt -= repay
                model_lots.append({"expires": expires, "seq": seq, "remaining": amount - repay})
            else:
                await debit(tenant, amount, f"d{step}")
                live = sorted(
                    (lot for lot in model_lots if lot["remaining"] > 0),
                    key=lambda lot: (lot["expires"] is None, lot["expires"] or datetime.max.replace(tzinfo=UTC), lot["seq"]),
                )
                takes, shortfall = credits.plan_allocation([(str(i), lot["remaining"]) for i, lot in enumerate(live)], amount)
                for index, take in takes:
                    live[int(index)]["remaining"] -= take
                model_debt += shortfall
            expected = sum((lot["remaining"] for lot in model_lots), D("0")) - model_debt
            assert (await credits.balance(tenant)).available == expected, f"diverged at step {step}"

        await assert_consistent(appdata_url, tenant)
