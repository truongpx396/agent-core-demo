"""Charging credits on a usage event, against a REAL Postgres
(postgres-init/19, 20 and 21; app/agent/usage_events.py, app/billing/credits.py, app/agent/budgets.py).

tests/billing/test_charge_on_event.py proves which statements run, on which connection and inside which
savepoint, through a fake. A fake cannot show what the DATABASE does with them, and every guarantee here is
database behaviour (constitution VII: reliance on a real constraint, lock or transaction is stated and
tested at this tier, not assumed):

  * the event and its debit are ONE transaction, so a replayed call is one event and one debit;
  * `UNIQUE (tenant, idempotency_key)` and the event's primary key arbitrate concurrent writers;
  * a debit that blows up part-way is rolled back by its SAVEPOINT without taking the event with it, and
    leaves no orphan transaction row behind;
  * 50 concurrent charges to one tenant lose no update, through the real event path (SC-003);
  * migration 21 adds the credit columns, refuses credits without a rate, and can be applied twice;
  * the gate reads the real wallet and the real in-flight holds: a tenant with no credits is refused, and
    a grant lets the next turn through (US3's independent test).

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg import errors as pg_errors

from app.agent import budget_holds, budgets, usage_events
from app.agent.pricing import ModelPrice, PricedCall
from app.billing import credits
from app.core import metrics
from tests.conftest import metric_value
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

D = Decimal
_REAL_INSERT = usage_events._insert
GATE = budgets.CreditGate(credits_per_usd=D("1000"), markup=D("1"), fail_policy="open")


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:  # commits on a normal exit
            yield conn

    for module in (usage_events, credits, budget_holds):
        monkeypatch.setattr(module, "get_connection", get_connection)
    monkeypatch.setattr(usage_events, "_insert", _REAL_INSERT)  # the autouse sink replaced it
    monkeypatch.setattr(usage_events, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage_events, "MARKUP", D("1"))


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


def ctx_for(tenant: str) -> dict:
    return {"tenant": tenant, "principal": "alice", "claims": {}}


def priced(cost_usd: float | None = 0.01) -> PricedCall:
    return PricedCall(100, 50, 0, 150, cost_usd, ModelPrice(0.0001, 0.0002) if cost_usd is not None else None)


async def call(tenant: str, message_id: str | None = None, cost_usd: float | None = 0.01) -> None:
    await usage_events.record_call(
        ctx_for(tenant), thread_id="t", message_id=message_id or uuid.uuid4().hex, kind="chat",
        model_alias="chat", priced=priced(cost_usd),
    )


async def fetch(url: str, sql: str, *params) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def scalar(url: str, sql: str, *params):
    return (await fetch(url, sql, *params))[0][0]


async def fund(tenant: str, amount="1000") -> None:
    await credits.grant(tenant, amount, source="purchase", idempotency_key=f"g-{tenant}", actor="test")


class TestTheEventAndItsDebitAreOneTransaction:
    async def test_a_call_is_one_event_rated_in_credits_and_one_debit_that_names_it(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await call(tenant, "m1", cost_usd=0.01)  # 0.01 USD x 1000 = 10 credits

        ((event_id, cost, rated, rate, markup),) = await fetch(
            appdata_url, "SELECT event_id, cost_usd, credits, credits_per_usd, markup FROM usage_events WHERE tenant = %s", tenant
        )
        assert (cost, rated, rate, markup) == (D("0.010000000000"), D("10.000000"), D("1000.000000"), D("1.000000"))
        ((kind, key, linked, tx_cost, tx_rate),) = await fetch(
            appdata_url,
            "SELECT kind, idempotency_key, usage_event_id, cost_usd, credits_per_usd FROM credit_transactions "
            "WHERE tenant = %s AND kind = 'debit'", tenant,
        )
        assert (kind, key, linked) == ("debit", event_id, event_id)
        assert (tx_cost, tx_rate) == (D("0.010000000000"), D("1000.000000")), "the debit records what it was priced from"
        assert (await credits.balance(tenant)).available == D("990.000000")
        assert await credits.verify(tenant) == []

    async def test_replaying_the_same_call_neither_adds_an_event_nor_charges_twice(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        for _ in range(3):
            await call(tenant, "same-message", cost_usd=0.01)

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 1
        assert (await credits.balance(tenant)).available == D("990.000000")

    async def test_twenty_concurrent_writers_of_one_call_charge_it_once(self, appdata_url):
        """The race a retried worker and a replayed turn would create: the primary key decides who inserts,
        and only the inserter debits."""
        tenant = tenant_name()
        await fund(tenant, "1000")

        await asyncio.gather(*[call(tenant, "contended", cost_usd=0.01) for _ in range(20)])

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 1
        assert (await credits.balance(tenant)).available == D("990.000000")

    async def test_fifty_concurrent_calls_lose_no_update_through_the_real_event_path(self, appdata_url):
        """SC-003, end to end: fifty different calls for one tenant, each its own event and debit."""
        tenant = tenant_name()
        await fund(tenant, "10000")

        await asyncio.gather(*[call(tenant, f"m{i}", cost_usd=0.01) for i in range(50)])

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 50
        balance = await credits.balance(tenant)
        assert balance.available == D("10000") - 50 * D("10"), "exactly fifty debits of ten credits"
        assert await credits.verify(tenant) == []
        entries = await scalar(appdata_url, "SELECT COALESCE(SUM(amount), 0) FROM credit_entries WHERE tenant = %s", tenant)
        assert entries == balance.ledger, "balance == SUM(entries)"

    async def test_a_tenant_with_no_wallet_gets_its_event_and_nothing_else(self, appdata_url):
        """Spec D8: no account means never debited, and a charge must not OPEN an account either."""
        tenant = tenant_name()

        await call(tenant, "m1")

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_accounts WHERE tenant = %s", tenant) == 0
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s", tenant) == 0

    async def test_usage_beyond_the_balance_is_booked_as_debt_never_refused(self, appdata_url):
        """The model call has already happened, so the debit records it: gating is a separate check."""
        tenant = tenant_name()
        await fund(tenant, "4")

        await call(tenant, "m1", cost_usd=0.01)  # 10 credits against 4

        balance = await credits.balance(tenant)
        assert (balance.available, balance.debt) == (D("-6.000000"), D("6.000000"))
        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1

    async def test_an_unpriced_call_is_recorded_with_null_credits_and_charges_nothing(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await call(tenant, "m1", cost_usd=None)

        ((cost, rated, rate),) = await fetch(
            appdata_url, "SELECT cost_usd, credits, credits_per_usd FROM usage_events WHERE tenant = %s", tenant
        )
        assert (cost, rated) == (None, None), "unknown stays unknown"
        assert rate == D("1000.000000"), "the rate in force is still recorded"
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 0
        assert (await credits.balance(tenant)).available == D("1000.000000")

    async def test_a_free_call_is_a_real_zero_and_writes_no_debit(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await call(tenant, "m1", cost_usd=0.0)

        assert await scalar(appdata_url, "SELECT credits FROM usage_events WHERE tenant = %s", tenant) == D("0")
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 0

    async def test_a_later_rate_change_never_rewrites_what_a_past_call_was_charged(self, appdata_url, monkeypatch):
        tenant = tenant_name()
        await fund(tenant, "100000")

        await call(tenant, "before", cost_usd=0.01)  # at 1000/USD: 10
        monkeypatch.setattr(usage_events, "CREDITS_PER_USD", D("2000"))
        monkeypatch.setattr(usage_events, "MARKUP", D("1.5"))
        await call(tenant, "after", cost_usd=0.01)  # at 2000/USD x 1.5: 30

        rows = await fetch(appdata_url, "SELECT credits, credits_per_usd, markup FROM usage_events WHERE tenant = %s ORDER BY credits", tenant)
        assert rows == [
            (D("10.000000"), D("1000.000000"), D("1.000000")),
            (D("30.000000"), D("2000.000000"), D("1.500000")),
        ]
        assert (await credits.balance(tenant)).available == D("100000") - 40

    async def test_a_cheap_call_is_charged_a_fraction_of_a_credit_not_nothing(self, appdata_url):
        """$0.0000004 at 1000 per dollar is 0.0004 credits: exact NUMERIC end to end, no float anywhere."""
        tenant = tenant_name()
        await fund(tenant, "1")

        await call(tenant, "m1", cost_usd=0.0000004)

        assert await scalar(appdata_url, "SELECT credits FROM usage_events WHERE tenant = %s", tenant) == D("0.000400")
        assert (await credits.balance(tenant)).available == D("0.999600")


class TestAWalletFaultNeverLosesTheEvent:
    @pytest.fixture
    def wallet_fault(self, monkeypatch):
        """A switch for a wallet that fails AFTER its debit has inserted its transaction row: the case a
        savepoint exists for, since it must undo that row and still leave the event. Off until a test
        has funded the wallet (a grant uses the same write)."""
        real_move = credits._move

        async def move_then_fail(conn, tenant, transaction_id, lot_id, amount):
            raise ConnectionError("the wallet fell over mid-debit")

        class Fault:
            def break_(self):
                monkeypatch.setattr(credits, "_move", move_then_fail)

            def heal(self):
                monkeypatch.setattr(credits, "_move", real_move)

        return Fault()

    async def test_the_event_stays_and_the_half_done_debit_leaves_nothing_behind(self, appdata_url, wallet_fault):
        tenant = tenant_name()
        await fund(tenant, "1000")
        wallet_fault.break_()
        before = metric_value(metrics.agent_cost_governance_degraded_total, path="credit_debit")
        write_before = metric_value(metrics.agent_cost_governance_degraded_total, path="usage_event_write")

        await call(tenant, "m1", cost_usd=0.01)  # does not raise

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1, "the meter outranks the charge"
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 0, (
            "the savepoint rolled the debit's own transaction row back: no orphan"
        )
        wallet_fault.heal()
        assert (await credits.balance(tenant)).available == D("1000.000000")
        assert await credits.verify(tenant) == []
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="credit_debit") == before + 1
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="usage_event_write") == write_before

    async def test_the_missed_charge_can_be_replayed_from_the_event_alone(self, appdata_url, wallet_fault):
        """The claim the design rests on: an uncharged event is repairable because the debit key IS the event
        id and the row holds every figure the debit needs. (Nothing does this automatically yet; the
        reconciliation is what will name the gap. This proves the repair works, not that it is scheduled.)"""
        tenant = tenant_name()
        await fund(tenant, "1000")
        wallet_fault.break_()
        await call(tenant, "m1", cost_usd=0.01)
        wallet_fault.heal()  # the wallet is healthy again

        ((event_id, rated, cost, rate, markup),) = await fetch(
            appdata_url, "SELECT event_id, credits, cost_usd, credits_per_usd, markup FROM usage_events WHERE tenant = %s", tenant
        )
        applied = await credits.debit(
            tenant, rated, idempotency_key=event_id, usage_event_id=event_id, actor="repair",
            pricing=credits.Pricing(cost, rate, markup),
        )
        again = await credits.debit(tenant, rated, idempotency_key=event_id, actor="repair")

        assert applied.status == "applied" and again.status == "duplicate"
        assert (await credits.balance(tenant)).available == D("990.000000")


class TestMigration21:
    async def test_credits_without_a_rate_are_refused_by_the_database(self, appdata_url):
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation, match="usage_events_credits_have_a_rate"):
                await conn.execute(
                    "INSERT INTO usage_events (event_id, tenant, principal, thread_id, kind, model_alias, credits) "
                    "VALUES (%s, %s, 'p', 't', 'chat', 'chat', 5)", (uuid.uuid4().hex, tenant_name()),
                )

    @pytest.mark.parametrize("column,value", [("credits", -1), ("credits_per_usd", 0), ("markup", -0.5)])
    async def test_a_nonsense_figure_is_refused(self, appdata_url, column, value):
        row = {"credits": 5, "credits_per_usd": 1000, "markup": 1, column: value}
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO usage_events (event_id, tenant, principal, thread_id, kind, model_alias, credits, credits_per_usd, markup) "
                    "VALUES (%s, %s, 'p', 't', 'chat', 'chat', %s, %s, %s)",
                    (uuid.uuid4().hex, tenant_name(), row["credits"], row["credits_per_usd"], row["markup"]),
                )

    async def test_the_new_columns_do_not_open_a_way_round_the_append_only_trigger(self, appdata_url):
        tenant = tenant_name()
        await call(tenant, "m1")

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            with pytest.raises(pg_errors.RaiseException, match="append-only"):
                await conn.execute("UPDATE usage_events SET credits = 0 WHERE tenant = %s", (tenant,))

    async def test_the_script_can_be_applied_twice(self, appdata_url):
        """An operator re-running it by hand against a volume that already has it must not fail."""
        script = Path(__file__).resolve().parents[2] / "postgres-init" / "21-usage-event-credits.sql"
        sql = "\n".join(line for line in script.read_text().splitlines() if not line.startswith("\\connect"))

        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute(sql)  # no exception

    async def test_an_event_written_without_credit_columns_still_works_after_the_migration(self, appdata_url, monkeypatch):
        """A deployment with no CREDITS_PER_USD keeps the original statement and the columns stay NULL."""
        monkeypatch.setattr(usage_events, "CREDITS_PER_USD", None)
        tenant = tenant_name()

        await call(tenant, "m1")

        assert await fetch(appdata_url, "SELECT credits, credits_per_usd, markup FROM usage_events WHERE tenant = %s", tenant) == [(None, None, None)]


class TestTheWalletRead:
    async def test_no_account_is_none_and_an_empty_account_is_zero(self):
        empty, none = tenant_name(), tenant_name()
        await credits.grant(empty, "5", source="manual", idempotency_key="g", actor="t")
        await credits.debit(empty, "5", idempotency_key="d")

        assert await credits.account_balance(none) is None
        assert (await credits.account_balance(empty)).available == D("0.000000")

    async def test_an_expired_lot_is_not_available_and_debt_is_subtracted(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, "10", source="promo", idempotency_key="live", actor="t", expires_at=datetime.now(UTC) + timedelta(days=1))
        await credits.grant(tenant, "7", source="promo", idempotency_key="old", actor="t", expires_at=datetime.now(UTC) + timedelta(seconds=1))
        await asyncio.sleep(1.2)

        wallet = await credits.account_balance(tenant)

        assert wallet.available == D("10.000000"), "an expired lot is never available, swept or not"
        assert wallet.ledger == D("17.000000")

    async def test_the_read_is_scoped_to_one_tenant(self):
        mine, other = tenant_name(), tenant_name()
        await fund(mine, "10")
        await fund(other, "999")

        assert (await credits.account_balance(mine)).available == D("10.000000")


class TestTheGateAgainstARealWallet:
    """US3's independent test: a tenant with no credits is refused with no model work, and a grant lets the
    next turn through."""

    async def check(self, tenant: str):
        return await budgets.check_allowance(ctx_for(tenant), limits=[], fail_policy="open", credit_gate=GATE)

    async def test_an_empty_wallet_is_refused_and_a_grant_lets_the_next_turn_through(self):
        tenant = tenant_name()
        await credits.grant(tenant, "1", source="manual", idempotency_key="g1", actor="t")
        await credits.debit(tenant, "1", idempotency_key="spend")

        assert (await self.check(tenant)).status == "insufficient_credits"

        await credits.grant(tenant, "500", source="purchase", idempotency_key="g2", actor="t")

        assert (await self.check(tenant)).status == "ok"

    async def test_a_tenant_with_no_wallet_is_never_gated(self):
        assert (await self.check(tenant_name())).status == "ok"

    async def test_a_real_in_flight_hold_counts_in_credits(self):
        """One running turn holds MAX_COST_USD_PER_TURN dollars; at 1000 credits per dollar that is 500, so
        a tenant with 100 cannot start a concurrent second turn that could spend more than it has."""
        tenant = tenant_name()
        await fund(tenant, "100")
        assert (await self.check(tenant)).status == "ok"

        hold = await budget_holds.reserve_budget(ctx_for(tenant), 0.50)
        try:
            assert hold is not None
            assert (await self.check(tenant)).status == "insufficient_credits"
        finally:
            await budget_holds.release_budget_reservation(ctx_for(tenant), hold)

        assert (await self.check(tenant)).status == "ok", "the hold is gone with the turn"

    async def test_a_charged_call_can_run_a_tenant_out_and_the_next_turn_is_refused(self):
        """The whole loop: the charge the event commits is what the gate reads."""
        tenant = tenant_name()
        await fund(tenant, "10")
        assert (await self.check(tenant)).status == "ok"

        await call(tenant, "m1", cost_usd=0.01)  # 10 credits: the wallet is now exactly empty

        assert (await self.check(tenant)).status == "insufficient_credits"
