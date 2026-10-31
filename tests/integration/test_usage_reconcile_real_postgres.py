"""The gateway reconciliation against a REAL Postgres (app/agent/cost/usage_reconcile.py; postgres-init/03, 19).

Every query it makes is the thing under test: the per-tenant, per-UTC-day grouping and the set of tenants the gateway's one-way ids are mapped
back to. A fake cursor would only echo the answers its author already believed (constitution VII). The gateway is the stand-in of
tests/agent/fake_gateway.py, built from LiteLLM's own source, fed from the rows this database really holds so the two sides can only disagree
where the test makes them. (The wallet's check, was every usage charged?, is the billing package's: its own real-Postgres test.)

THE headline (specs/010 T026): delete one usage event and the report names the tenant, the day and the amount.

The database is shared across tests and xdist workers, and the reconciliation reads every tenant's rows, so every assertion is about
ITS OWN tenant's findings; another test's tenant showing up as drift is correct and irrelevant.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from app.adapters.litellm.spend_logs import HttpGatewaySpendLog
from app.agent.cost import usage_events
from app.agent.cost import usage_reconcile as reconcile
from app.agent.cost.pricing import ModelPrice, PricedCall
from app.agent.cost.usage_reconcile import Tolerance
from app.core.gateway import end_user_id, legacy_end_user_id
from app.ports.cost import Window
from tests.agent.fake_gateway import FakeGateway, spend_row
from tests.containers import ensure_postgres
from tests.integration.postgres_adapters import use_real_postgres
from tests.integration.usage_seed import seed_event

pytestmark = pytest.mark.integration

D = Decimal
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

    use_real_postgres(monkeypatch, get_connection)


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


def ctx_for(tenant: str, principal: str = "alice") -> dict:
    return {"tenant": tenant, "principal": principal, "claims": {}}


def priced(cost_usd: float) -> PricedCall:
    return PricedCall(100, 50, 0, 150, cost_usd, ModelPrice(0.0001, 0.0002))


async def turn(tenant: str, cost_usd: float, principal: str = "alice") -> str:
    """One model call the way the app records it: a usage event (which is also what the dollar caps sum)."""
    message_id = uuid.uuid4().hex
    await usage_events.record_call(
        ctx_for(tenant, principal), thread_id="t", message_id=message_id, kind="chat", model_alias="chat", priced=priced(cost_usd)
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
    """What a faithful gateway would hold for these tenants RIGHT NOW: one spend row per usage event they have, under the end user the app
    sends for that event's person."""
    rows = []
    for tenant in tenants:
        for event_id, occurred_at, cost, principal in await fetch(
            url, "SELECT event_id, occurred_at, cost_usd, principal FROM usage_events WHERE tenant = %s", tenant
        ):
            rows.append(spend_row(end_user_id(tenant, principal), occurred_at, float(cost), f"req-{event_id}"))
    return FakeGateway(rows)


async def reconcile_for(gateway: FakeGateway | None, tenant: str, **kwargs) -> list[reconcile.Finding]:
    kwargs.setdefault("tolerance", STRICT)
    if gateway is None:
        report = await reconcile.reconcile(window(), **kwargs)
    else:
        async with gateway.client() as client:
            report = await reconcile.reconcile(window(), spend_log=HttpGatewaySpendLog(client), **kwargs)
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

    async def test_a_tenant_whose_every_event_is_gone_is_named_because_the_gateways_end_user_carries_its_name(self, appdata_url):
        """The end user names the person (`tenant:<tenant>/user:<principal>`), so the tenant is read off the id:
        nothing in the database has to remember it.
        """
        gone = tenant_name()
        only = await turn(gone, 0.05)
        gateway = await gateway_seen(appdata_url, gone)
        await delete_event(appdata_url, only)

        findings = await reconcile_for(gateway, gone)

        assert [(f.kind, f.drift) for f in findings] == [("gateway", D("0.05"))]

    async def test_one_tenants_people_are_summed_into_one_tenant_day(self, appdata_url):
        """The gateway has one end user per person; the reconciliation is per tenant, so two people's spend meets two people's events."""
        tenant = tenant_name()
        await turn(tenant, 0.05, principal="alice")
        await turn(tenant, 0.07, principal="bob")
        gateway = await gateway_seen(appdata_url, tenant)

        assert await reconcile_for(gateway, tenant) == []

    async def test_a_tenant_with_earlier_events_and_none_in_the_window_is_named_not_left_as_a_hash_in_an_older_spend_log(self, appdata_url):
        """Spend-log rows written before the end user named the person carry the one-way `tenant_<hash>` id. The tenant is known to the
        database by an event from before the window, which is how that id is mapped back."""
        tenant = tenant_name()
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            await seed_event(conn, tenant, occurred_at=datetime.now(UTC) - timedelta(days=10))
        gateway = FakeGateway([spend_row(legacy_end_user_id(tenant), datetime.now(UTC), 0.30, "orphan-call")])

        findings = await reconcile_for(gateway, tenant)

        assert [(f.kind, f.drift) for f in findings] == [("gateway", D("0.3"))]

    async def test_an_older_hashed_id_no_tenant_hashes_to_is_still_a_finding_named_by_that_id(self, appdata_url):
        gone = tenant_name()
        gateway = FakeGateway([spend_row(legacy_end_user_id(gone), datetime.now(UTC), 0.20, "old-orphan")])

        findings = await reconcile_for(gateway, f"{legacy_end_user_id(gone)} (no tenant in this database hashes to it)")

        assert [(f.kind, f.drift) for f in findings] == [("gateway", D("0.2"))]
