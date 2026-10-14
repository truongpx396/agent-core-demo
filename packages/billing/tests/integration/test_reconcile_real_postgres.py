"""The wallet reconciliation against a REAL Postgres (credit_billing/reconcile.py; sql/20, 24; the wallet half of the agent app's, moved by specs/013).

Every query it makes is the thing under test: the per-tenant, per-UTC-day grouping, `ON` the tenant, the lookup of a debit by the event id, the
`recorded_at >= created_at` rule that an event older than its wallet was rightly never charged. A fake cursor would only echo the answers its
author already believed (constitution VII).

THE headline: let one usage's debit fail and the report names the tenant, the day and the amount.

The database is shared across tests and xdist workers, and the reconciliation reads every tenant's rows, so every assertion is about ITS OWN
tenant's findings; another test's tenant showing up as drift is correct and irrelevant.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from credit_billing import credits, reconcile, usage
from credit_billing.usage import Usage
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

    for module in (credits, reconcile):
        monkeypatch.setattr(module, "get_connection", get_connection)
    monkeypatch.setattr(usage, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage, "MARKUP", D("1"))
    monkeypatch.setattr(usage, "BILLING_PROVIDERS", ())


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


async def turn(url: str, tenant: str, cost_usd: float) -> str:
    """One recorded usage, the way a host records it: in a transaction that already holds its own statement."""
    event_id = uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(url) as conn:
        await conn.execute("SELECT 1")  # the host's own insert opens the transaction the package nests in
        await usage.record_usage_in(conn, Usage(tenant, event_id, "chat", cost_usd))
    return event_id


async def fetch(url: str, sql: str, *params) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


def window() -> reconcile.Window:
    now = datetime.now(UTC).replace(microsecond=0)
    return reconcile.Window(now - timedelta(days=2), now + timedelta(minutes=5))


async def findings_for(tenant: str) -> list[reconcile.Finding]:
    return [f for f in (await reconcile.reconcile(window())).findings if f.tenant == tenant]


def failing_wallet(monkeypatch):
    real_debit = credits.debit_in

    async def broken(*args, **kwargs):
        raise psycopg.OperationalError("the wallet is down")

    monkeypatch.setattr(credits, "debit_in", broken)
    return lambda: monkeypatch.setattr(credits, "debit_in", real_debit)


class TestWhenEverythingWasCharged:
    async def test_a_tenants_recorded_usage_reconciles_to_nothing(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        for cost in (0.01, 0.02, 0.04):
            await turn(appdata_url, tenant, cost)

        assert await findings_for(tenant) == []

    async def test_the_header_counts_what_was_read(self, appdata_url):
        tenant = tenant_name()
        await turn(appdata_url, tenant, 0.01)

        report = await reconcile.reconcile(window())

        assert report.events >= 1 and report.tenants >= 1 and report.outcome in ("ok", "drift")


class TestAnUnchargedEvent:
    async def test_an_event_the_wallet_failed_to_charge_is_named_and_a_repair_clears_it(self, appdata_url, monkeypatch):
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        await turn(appdata_url, tenant, 0.01)  # charged: the control, which must NOT be reported
        heal = failing_wallet(monkeypatch)
        uncharged = await turn(appdata_url, tenant, 0.25)  # the record keeps the event; the charge is lost
        heal()
        day = (await fetch(appdata_url, "SELECT (occurred_at AT TIME ZONE 'UTC')::date FROM billing_usage_events WHERE tenant = %s AND event_id = %s", tenant, uncharged))[0][0]

        (finding,) = await findings_for(tenant)

        assert (finding.tenant, finding.day, finding.events, finding.credits, finding.usd) == (tenant, day, 1, D("250.000000"), D("0.250000000000"))
        assert "1 event(s) worth 250.000000 credits" in reconcile.render(reconcile.Report(window(), findings=[finding]))

        # What a repair does, and the proof the detector looks where the charge writes: the debit key IS the event id.
        await credits.debit(tenant, D("250"), idempotency_key=uncharged, usage_event_id=uncharged, actor="repair")

        assert await findings_for(tenant) == []

    async def test_the_documented_manual_repair_clears_the_finding_and_charges_exactly_once(self, appdata_url, monkeypatch):
        """The runbook tells an operator to book `credit-billing adjust --amount=-<credits> --key <event_id>`; that recipe is only worth printing if
        it works: the finding clears (the debit key IS the event id), the wallet drops by the event's credits once, and a second run of the same
        command is a duplicate."""
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        heal = failing_wallet(monkeypatch)
        event_id = await turn(appdata_url, tenant, 0.25)
        heal()
        owed = (await fetch(appdata_url, "SELECT credits FROM billing_usage_events WHERE tenant = %s AND event_id = %s", tenant, event_id))[0][0]
        assert len(await findings_for(tenant)) == 1

        first = await credits.adjust(tenant, -owed, idempotency_key=event_id, actor="operator:alice", reason=f"uncharged event {event_id}")
        again = await credits.adjust(tenant, -owed, idempotency_key=event_id, actor="operator:alice", reason=f"uncharged event {event_id}")

        assert (first.status, again.status) == ("applied", "duplicate")
        assert (await credits.balance(tenant)).available == D("1000") - owed
        assert await findings_for(tenant) == []

    async def test_a_call_worth_less_than_a_millionth_of_a_credit_is_never_reported_as_uncharged(self, appdata_url):
        """At 1000 credits per dollar a $0.0000000004 call is worth 0.0000004 credits, which rounds to zero at six places. A debit of zero books no
        transaction by design, so its absence is not a gap: only the `credits > 0` rule keeps it out of the report."""
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        await turn(appdata_url, tenant, 4e-10)
        assert await fetch(appdata_url, "SELECT credits, cost_usd FROM billing_usage_events WHERE tenant = %s", tenant) == [(D("0.000000"), D("0.000000000400"))]

        assert await findings_for(tenant) == []

    async def test_an_event_recorded_before_the_wallet_existed_was_rightly_never_charged(self, appdata_url):
        tenant = tenant_name()
        await turn(appdata_url, tenant, 0.10)  # no wallet yet
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")

        assert await findings_for(tenant) == []

    async def test_an_unpriced_call_owes_nothing_and_is_never_a_finding(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT 1")
            await usage.record_usage_in(conn, Usage(tenant, "unpriced", "chat", None))

        assert await findings_for(tenant) == []

    async def test_another_tenants_debit_with_the_same_key_never_hides_the_gap(self, appdata_url, monkeypatch):
        mine, other = tenant_name(), tenant_name()
        await credits.grant(mine, 1000, source="purchase", idempotency_key=f"g-{mine}", actor="test")
        await credits.grant(other, 1000, source="purchase", idempotency_key=f"g-{other}", actor="test")
        heal = failing_wallet(monkeypatch)
        event_id = await turn(appdata_url, mine, 0.25)
        heal()
        await credits.debit(other, D("250"), idempotency_key=event_id, usage_event_id=event_id, actor="test")  # same key, other tenant

        assert len(await findings_for(mine)) == 1

    async def test_the_same_event_id_under_two_tenants_is_judged_per_tenant(self, appdata_url, monkeypatch):
        """The tenant is part of the event's key, so the two are two events and only the uncharged one is a finding."""
        charged, lost = tenant_name(), tenant_name()
        for who in (charged, lost):
            await credits.grant(who, 1000, source="purchase", idempotency_key=f"g-{who}", actor="test")
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT 1")
            await usage.record_usage_in(conn, Usage(charged, "shared", "chat", 0.01))
        heal = failing_wallet(monkeypatch)
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await conn.execute("SELECT 1")
            await usage.record_usage_in(conn, Usage(lost, "shared", "chat", 0.01))
        heal()

        assert await findings_for(charged) == [] and len(await findings_for(lost)) == 1


class TestTheWalletTotals:
    async def test_credits_still_usable_and_credits_owed_are_reported_apart(self):
        spent, owing, untouched = tenant_name(), tenant_name(), tenant_name()
        await credits.grant(spent, 100, source="purchase", idempotency_key=f"g-{spent}", actor="test")
        await credits.debit(spent, 130, idempotency_key=f"d-{spent}", actor="test")  # 30 more than it holds: booked as debt
        await credits.grant(owing, 40, source="promo", idempotency_key=f"g-{owing}", actor="test", expires_at=datetime.now(UTC) + timedelta(days=1))
        await credits.grant(untouched, 7, source="purchase", idempotency_key=f"g-{untouched}", actor="test")

        assert await reconcile.wallet_totals(spent) == (D("0"), D("30"))
        assert await reconcile.wallet_totals(owing) == (D("40"), D("0"))
        assert await reconcile.wallet_totals(untouched) == (D("7"), D("0"))
        live, debt = await reconcile.wallet_totals()
        assert live >= D("47") and debt >= D("30")  # the whole deployment, which other tests add to

    async def test_a_lot_past_its_expiry_is_not_usable_even_before_the_sweep_has_booked_it(self):
        tenant = tenant_name()
        await credits.grant(tenant, 50, source="promo", idempotency_key=f"g-{tenant}", actor="test", expires_at=datetime.now(UTC) + timedelta(seconds=1.5))
        assert await reconcile.wallet_totals(tenant) == (D("50"), D("0"))

        await asyncio.sleep(2)

        assert await reconcile.wallet_totals(tenant) == (D("0"), D("0"))  # the sweep has not run, and the credits are already not outstanding
