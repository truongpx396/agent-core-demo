"""The agent's meter and billing, end to end against a REAL Postgres (app/agent/cost/usage_events.py ->
app/billing_integration/port.py -> the billing package; postgres-init/19, 20, 24, 25; specs/013). What billing
DOES with a recorded usage (rating, atomic debit, savepoints, export row, schema guards) is proven by the
package's own tests (packages/billing/tests/integration/). Only this file shows the SEAM, whole:
- the agent's event and billing's record and debit commit as ONE transaction, so a replayed model call is one
  of each;
- the agent's table has no billing column on a fresh database;
- a billing fault never costs the agent its own event, and billing being off writes nothing of its own;
- the credit gate reads the real wallet and in-flight holds: a tenant with no credits is refused, a grant lets
  the next turn through (US3's independent test), and a call the meter charged is what runs it out.

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest

from app.agent.cost import budget_holds, budgets, usage_events
from app.agent.cost.pricing import ModelPrice, PricedCall
from app.billing_integration import port as billing_port
from app.core import metrics
from credit_billing import admission, credits, usage
from credit_billing import metrics as billing_metrics
from tests.conftest import metric_value
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

D = Decimal
_REAL_INSERT = usage_events._insert


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
    monkeypatch.setattr(billing_port, "billing", billing_port.InProcessBilling())  # a test above may have installed a stub
    monkeypatch.setattr(usage, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(usage, "MARKUP", D("1"))
    monkeypatch.setattr(usage, "BILLING_PROVIDERS", ())
    monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", True)
    monkeypatch.setattr(admission, "CREDITS_PER_USD", D("1000"))
    monkeypatch.setattr(admission, "MARKUP", D("1"))
    monkeypatch.setattr(admission, "CREDIT_CHECK_FAILURE_POLICY", "open")


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


def ctx_for(tenant: str) -> dict:
    return {"tenant": tenant, "principal": "alice", "claims": {}}


def priced(cost_usd: float | None = 0.01) -> PricedCall:
    return PricedCall(100, 50, 0, 150, cost_usd, ModelPrice(0.0001, 0.0002) if cost_usd is not None else None)


async def call(tenant: str, message_id: str | None = None, cost_usd: float | None = 0.01) -> None:
    await usage_events.record_call(
        ctx_for(tenant), thread_id="t", message_id=message_id or uuid.uuid4().hex, kind="chat", model_alias="chat", priced=priced(cost_usd)
    )


async def fetch(url: str, sql: str, *params) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def scalar(url: str, sql: str, *params):
    return (await fetch(url, sql, *params))[0][0]


async def fund(tenant: str, amount="1000") -> None:
    await credits.grant(tenant, amount, source="purchase", idempotency_key=f"g-{tenant}", actor="test")


class TestTheAgentsEventAndBillingsRecordAreOneTransaction:
    async def test_a_call_is_one_agent_event_one_billing_record_and_one_debit_that_names_it(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await call(tenant, "m1", cost_usd=0.01)  # 0.01 USD x 1000 = 10 credits

        ((event_id,),) = await fetch(appdata_url, "SELECT event_id FROM usage_events WHERE tenant = %s", tenant)
        ((billed_id, rated, rate, model, tokens),) = await fetch(
            appdata_url, "SELECT event_id, credits, credits_per_usd, model, tokens FROM billing_usage_events WHERE tenant = %s", tenant
        )
        assert billed_id == event_id, "billing is handed the agent's own event id"
        assert (rated, rate, model, tokens) == (D("10.000000"), D("1000.000000"), "chat", 150)
        ((key, linked),) = await fetch(
            appdata_url, "SELECT idempotency_key, usage_event_id FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant
        )
        assert (key, linked) == (event_id, event_id)
        assert (await credits.balance(tenant)).available == D("990.000000")
        assert await credits.verify(tenant) == []

    async def test_replaying_the_same_call_neither_adds_an_event_nor_a_record_nor_charges_twice(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        for _ in range(3):
            await call(tenant, "same-message")

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 1
        assert (await credits.balance(tenant)).available == D("990.000000")

    async def test_twenty_concurrent_writers_of_one_call_charge_it_once(self, appdata_url):
        """The race a retried worker and a replayed turn would create: the agent's primary key decides who inserts, and only the inserter hands over."""
        tenant = tenant_name()
        await fund(tenant, "1000")

        await asyncio.gather(*[call(tenant, "contended") for _ in range(20)])

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_transactions WHERE tenant = %s AND kind = 'debit'", tenant) == 1

    async def test_fifty_concurrent_calls_lose_no_update_through_the_real_event_path(self, appdata_url):
        """SC-003, end to end: fifty different calls for one tenant, each its own event and debit."""
        tenant = tenant_name()
        await fund(tenant, "10000")

        await asyncio.gather(*[call(tenant, f"m{i}") for i in range(50)])

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 50
        balance = await credits.balance(tenant)
        assert balance.available == D("10000") - 50 * D("10"), "exactly fifty debits of ten credits"
        assert await credits.verify(tenant) == []

    async def test_a_tenant_with_no_wallet_gets_its_events_and_nothing_else(self, appdata_url):
        """No account means never debited, and a charge must not OPEN an account either."""
        tenant = tenant_name()

        await call(tenant, "m1")

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM credit_accounts WHERE tenant = %s", tenant) == 0

    async def test_an_unpriced_call_is_an_agent_event_with_null_cost_and_a_billing_record_with_null_credits(self, appdata_url):
        tenant = tenant_name()
        await fund(tenant, "1000")

        await call(tenant, "m1", cost_usd=None)

        assert await fetch(appdata_url, "SELECT cost_usd FROM usage_events WHERE tenant = %s", tenant) == [(None,)]
        assert await fetch(appdata_url, "SELECT cost_usd, credits FROM billing_usage_events WHERE tenant = %s", tenant) == [(None, None)]
        assert (await credits.balance(tenant)).available == D("1000.000000")


class TestTheAgentsTableCarriesNoBilling:
    async def test_a_fresh_database_gives_usage_events_no_credit_column(self, appdata_url):
        columns = {row[0] for row in await fetch(appdata_url, "SELECT column_name FROM information_schema.columns WHERE table_name = 'usage_events'")}

        assert columns.isdisjoint({"credits", "credits_per_usd", "markup"})

    async def test_a_fresh_database_has_no_agent_export_outbox(self, appdata_url):
        assert await scalar(appdata_url, "SELECT to_regclass('usage_export_outbox') IS NULL")

    async def test_with_billing_off_the_agent_writes_its_row_and_billing_writes_nothing(self, appdata_url, monkeypatch):
        monkeypatch.setattr(usage, "CREDITS_PER_USD", None)
        tenant = tenant_name()

        await call(tenant, "m1")

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 0


class TestABillingFaultNeverLosesTheAgentsEvent:
    async def test_a_wallet_fault_keeps_both_events_and_is_counted_on_the_package_path(self, appdata_url, monkeypatch):
        tenant = tenant_name()
        await fund(tenant, "1000")

        async def broken(*args, **kwargs):
            raise psycopg.OperationalError("the wallet is down")

        monkeypatch.setattr(credits, "debit_in", broken)
        before = metric_value(billing_metrics.agent_cost_governance_degraded_total, path="credit_debit")
        write_before = metric_value(metrics.agent_cost_governance_degraded_total, path="usage_event_write")

        await call(tenant, "m1")  # does not raise

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1
        assert await scalar(appdata_url, "SELECT count(*) FROM billing_usage_events WHERE tenant = %s", tenant) == 1
        assert metric_value(billing_metrics.agent_cost_governance_degraded_total, path="credit_debit") == before + 1
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="usage_event_write") == write_before

    async def test_an_unapplied_billing_schema_costs_the_agent_nothing_and_is_counted(self, appdata_url, monkeypatch):
        tenant = tenant_name()
        monkeypatch.setattr(usage, "_INSERT", usage._INSERT.replace("billing_usage_events", "billing_usage_events_not_applied"))
        before = metric_value(billing_metrics.agent_cost_governance_degraded_total, path="billing_event_write")

        await call(tenant, "m1")

        assert await scalar(appdata_url, "SELECT count(*) FROM usage_events WHERE tenant = %s", tenant) == 1, "the agent's own meter is untouched"
        assert metric_value(billing_metrics.agent_cost_governance_degraded_total, path="billing_event_write") == before + 1


class TestTheGateAgainstARealWallet:
    """US3's independent test: a tenant with no credits is refused with no model work, and a grant lets the next turn through."""

    async def check(self, tenant: str):
        return await budgets.check_allowance(ctx_for(tenant), limits=[], fail_policy="open")

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
        """One running turn holds MAX_COST_USD_PER_TURN dollars; at 1000 credits per dollar that is 500, so a tenant with 100 cannot start a
        concurrent second turn that could spend more than it has."""
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
        """The whole loop: the charge the meter's hand-off commits is what the gate reads."""
        tenant = tenant_name()
        await fund(tenant, "10")
        assert (await self.check(tenant)).status == "ok"

        await call(tenant, "m1", cost_usd=0.01)  # 10 credits: the wallet is now exactly empty

        assert (await self.check(tenant)).status == "insufficient_credits"

    async def test_with_enforcement_off_the_same_empty_wallet_is_served(self, monkeypatch):
        monkeypatch.setattr(admission, "CREDITS_ENFORCEMENT", False)
        tenant = tenant_name()
        await credits.grant(tenant, "1", source="manual", idempotency_key="g1", actor="t")
        await credits.debit(tenant, "1", idempotency_key="spend")

        assert (await self.check(tenant)).status == "ok"
