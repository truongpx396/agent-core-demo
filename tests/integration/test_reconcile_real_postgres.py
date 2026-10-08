"""The reconciliation against a REAL Postgres (app/billing/reconcile.py; postgres-init/03, 19, 20, 21).

Every query it makes is the thing under test: the per-tenant, per-UTC-day grouping, `ON` the tenant, the lookup of a debit by the
event id, the `recorded_at >= created_at` rule that an event older than its wallet was rightly never charged. A fake cursor would only
echo the answers its author already believed (constitution VII). The gateway is the stand-in of tests/billing/fake_gateway.py, built
from LiteLLM's own source, fed from the rows this database really holds so the two sides can only disagree where the test makes them.

THE headline (specs/010 T026): delete one usage event and the report names the tenant, the day and the amount.

The database is shared across tests and xdist workers, and the reconciliation reads every tenant's rows, so every assertion is about
ITS OWN tenant's findings; another test's tenant showing up as drift is correct and irrelevant.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from app.agent import usage_events
from app.agent.gateway import end_user_id
from app.agent.pricing import ModelPrice, PricedCall
from app.billing import credits, reconcile
from app.billing.reconcile import Tolerance, Window
from tests.billing.fake_gateway import FakeGateway, spend_row
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

D = Decimal
_REAL_INSERT = usage_events._insert
STRICT = Tolerance(usd=D("0.0001"), pct=D("0"))


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:  # commits on a normal exit
            yield conn

    for module in (usage_events, credits, reconcile):
        monkeypatch.setattr(module, "get_connection", get_connection)
    monkeypatch.setattr(usage_events, "_insert", _REAL_INSERT)  # the autouse sink replaced it
    monkeypatch.setattr(usage_events, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage_events, "MARKUP", D("1"))


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


def ctx_for(tenant: str) -> dict:
    return {"tenant": tenant, "principal": "alice", "claims": {}}


def priced(cost_usd: float) -> PricedCall:
    return PricedCall(100, 50, 0, 150, cost_usd, ModelPrice(0.0001, 0.0002))


async def turn(tenant: str, cost_usd: float) -> str:
    """One model call the way the app records it: a usage event (which is also what the dollar caps sum)."""
    message_id = uuid.uuid4().hex
    await usage_events.record_call(
        ctx_for(tenant), thread_id="t", message_id=message_id, kind="chat", model_alias="chat", priced=priced(cost_usd)
    )
    return usage_events.event_id_for(tenant, message_id)


async def fetch(url: str, sql: str, *params) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


def window() -> Window:
    now = datetime.now(UTC).replace(microsecond=0)
    return Window(now - timedelta(days=2), now + timedelta(minutes=5))


async def gateway_seen(url: str, *tenants: str) -> FakeGateway:
    """What a faithful gateway would hold for these tenants RIGHT NOW: one spend row per usage event they have."""
    rows = []
    for tenant in tenants:
        for event_id, occurred_at, cost in await fetch(url, "SELECT event_id, occurred_at, cost_usd FROM usage_events WHERE tenant = %s", tenant):
            rows.append(spend_row(end_user_id(tenant), occurred_at, float(cost), f"req-{event_id}"))
    return FakeGateway(rows)


async def reconcile_for(gateway: FakeGateway | None, tenant: str, **kwargs) -> list[reconcile.Finding]:
    kwargs.setdefault("tolerance", STRICT)
    if gateway is None:
        report = await reconcile.reconcile(window(), **kwargs)
    else:
        async with gateway.client() as client:
            report = await reconcile.reconcile(window(), gateway_client=client, **kwargs)
    return [f for f in report.findings if f.tenant == tenant]


async def delete_event(url: str, event_id: str) -> None:
    """The one sanctioned way, the retention job's: the trigger refuses a delete that does not say so inside its own transaction."""
    async with await psycopg.AsyncConnection.connect(url) as conn:
        await conn.execute("SET LOCAL usage_events.allow_delete = 'on'")
        await conn.execute("DELETE FROM usage_events WHERE event_id = %s", (event_id,))


async def utc_day_of(url: str, event_id: str):
    return (await fetch(url, "SELECT (occurred_at AT TIME ZONE 'UTC')::date FROM usage_events WHERE event_id = %s", event_id))[0][0]


class TestWhenTheRecordsAgree:
    async def test_a_tenants_turns_reconcile_to_nothing_against_the_gateway(self, appdata_url):
        tenant = tenant_name()
        for cost in (0.01, 0.02, 0.04):
            await turn(tenant, cost)

        assert await reconcile_for(await gateway_seen(appdata_url, tenant), tenant) == []


class TestADeletedEvent:
    async def test_the_report_names_the_tenant_the_day_and_the_amount(self, appdata_url):
        tenant = tenant_name()
        await turn(tenant, 0.01)
        lost = await turn(tenant, 0.02)
        await turn(tenant, 0.04)
        gateway = await gateway_seen(appdata_url, tenant)  # the gateway saw all three calls
        day = await utc_day_of(appdata_url, lost)

        await delete_event(appdata_url, lost)
        findings = await reconcile_for(gateway, tenant)

        (finding,) = findings  # the gateway still holds what the meter lost
        assert (finding.kind, finding.tenant, finding.day, finding.drift) == ("gateway", tenant, day, D("0.02"))
        report = reconcile.Report(window(), STRICT, findings=findings)
        text = reconcile.render(report)
        assert tenant in text and str(day) in text and "0.020000" in text

    async def test_it_is_that_tenants_day_only_never_another_tenants(self, appdata_url):
        mine, other = tenant_name(), tenant_name()
        await turn(mine, 0.01)
        lost = await turn(mine, 0.05)
        await turn(other, 0.05)
        gateway = await gateway_seen(appdata_url, mine, other)

        await delete_event(appdata_url, lost)

        assert [f.tenant for f in await reconcile_for(gateway, mine)] == [mine]
        assert await reconcile_for(gateway, other) == []

    async def test_a_tenant_whose_every_event_is_gone_and_has_no_wallet_is_named_by_its_gateway_id(self, appdata_url):
        """Since the ledger stopped being compared there is nothing left in the database that knows such a tenant's name (a ledger
        row used to), so the gateway's one-way id is all the report can give. That is the documented fallback: the runbook
        says how to turn the hash back into a name (`litellm_key end-user --tenant <name>`)."""
        gone = tenant_name()
        only = await turn(gone, 0.05)
        gateway = await gateway_seen(appdata_url, gone)
        await delete_event(appdata_url, only)

        findings = await reconcile_for(gateway, f"{end_user_id(gone)} (no tenant in this database hashes to it)")

        assert [(f.kind, f.drift) for f in findings] == [("gateway", D("0.05"))]

    async def test_a_tenant_with_a_wallet_and_every_event_gone_is_named_not_left_as_a_hash(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, 10, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        gateway = FakeGateway([spend_row(end_user_id(tenant), datetime.now(UTC), 0.30, "orphan-call")])

        findings = await reconcile_for(gateway, tenant)

        assert [(f.kind, f.drift) for f in findings] == [("gateway", D("0.3"))]  # `credit_accounts` is how the hash was mapped back


class TestAnUnchargedEvent:
    async def test_an_event_the_wallet_failed_to_charge_is_named_and_a_repair_clears_it(self, appdata_url, monkeypatch):
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        await turn(tenant, 0.01)  # charged: the control, which must NOT be reported

        real_debit = credits.debit_in

        async def broken(*args, **kwargs):
            raise psycopg.OperationalError("the wallet is down")

        monkeypatch.setattr(credits, "debit_in", broken)
        uncharged = await turn(tenant, 0.25)  # the meter keeps the event; the charge is lost (D11)
        monkeypatch.setattr(credits, "debit_in", real_debit)
        gateway = await gateway_seen(appdata_url, tenant)
        day = await utc_day_of(appdata_url, uncharged)

        (finding,) = await reconcile_for(gateway, tenant)

        assert (finding.kind, finding.tenant, finding.day, finding.actual) == ("uncharged", tenant, day, D("0.25"))
        assert "1 event(s) worth 250.000000 credits were never debited" in finding.note

        # What a repair job would do, and the proof the detector looks where the charge writes: the debit key IS the event id.
        await credits.debit(tenant, D("250"), idempotency_key=uncharged, usage_event_id=uncharged, actor="repair")

        assert await reconcile_for(gateway, tenant) == []

    async def test_the_documented_manual_repair_clears_the_finding_and_charges_exactly_once(self, appdata_url, monkeypatch):
        """infra/README.md tells an operator to book `credits adjust --amount=-<credits> --key <event_id>`; that recipe is only worth
        printing if it works: the finding clears (the debit key IS the event id), the wallet drops by the event's credits once, and a
        second run of the same command is a duplicate."""
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        real_debit = credits.debit_in

        async def broken(*args, **kwargs):
            raise psycopg.OperationalError("the wallet is down")

        monkeypatch.setattr(credits, "debit_in", broken)
        event_id = await turn(tenant, 0.25)
        monkeypatch.setattr(credits, "debit_in", real_debit)
        gateway = await gateway_seen(appdata_url, tenant)
        owed = (await fetch(appdata_url, "SELECT credits FROM usage_events WHERE event_id = %s", event_id))[0][0]
        assert [f.kind for f in await reconcile_for(gateway, tenant)] == ["uncharged"]

        first = await credits.adjust(tenant, -owed, idempotency_key=event_id, actor="operator:alice", reason=f"uncharged event {event_id}")
        again = await credits.adjust(tenant, -owed, idempotency_key=event_id, actor="operator:alice", reason=f"uncharged event {event_id}")

        assert (first.status, again.status) == ("applied", "duplicate")
        assert (await credits.balance(tenant)).available == D("1000") - owed
        assert await reconcile_for(gateway, tenant) == []

    async def test_a_call_worth_less_than_a_millionth_of_a_credit_is_never_reported_as_uncharged(self, appdata_url):
        """At 1000 credits per dollar a $0.0000000004 call is worth 0.0000004 credits, which rounds to zero at six places. A debit of zero
        books no transaction by design, so its absence is not a gap; but the event's own cost is above zero, so only the `credits > 0`
        rule keeps it out of the report (a free call, cost 0, could never tell the difference: its drift would be zero either way)."""
        tenant = tenant_name()
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        await turn(tenant, 4e-10)
        assert (await fetch(appdata_url, "SELECT credits, cost_usd FROM usage_events WHERE tenant = %s", tenant)) == [(D("0.000000"), D("0.000000000400"))]

        assert await reconcile_for(await gateway_seen(appdata_url, tenant), tenant, tolerance=Tolerance(D("0.1"), D("100"))) == []

    async def test_an_event_recorded_before_the_wallet_existed_was_rightly_never_charged(self, appdata_url):
        tenant = tenant_name()
        await turn(tenant, 0.10)  # no wallet yet
        await credits.grant(tenant, 1000, source="purchase", idempotency_key=f"g-{tenant}", actor="test")

        assert await reconcile_for(await gateway_seen(appdata_url, tenant), tenant) == []

    async def test_another_tenants_debit_with_the_same_key_never_hides_the_gap(self, appdata_url, monkeypatch):
        mine, other = tenant_name(), tenant_name()
        await credits.grant(mine, 1000, source="purchase", idempotency_key=f"g-{mine}", actor="test")
        await credits.grant(other, 1000, source="purchase", idempotency_key=f"g-{other}", actor="test")
        real_debit = credits.debit_in

        async def broken(*args, **kwargs):
            raise psycopg.OperationalError("down")

        monkeypatch.setattr(credits, "debit_in", broken)
        event_id = await turn(mine, 0.25)
        monkeypatch.setattr(credits, "debit_in", real_debit)
        await credits.debit(other, D("250"), idempotency_key=event_id, usage_event_id=event_id, actor="test")  # same key, other tenant

        findings = await reconcile_for(await gateway_seen(appdata_url, mine), mine)

        assert [f.kind for f in findings] == ["uncharged"]


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
