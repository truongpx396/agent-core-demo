"""Money in, against a REAL Postgres (postgres-init/20 and 22; app/billing/webhooks.py, store.py, inbox.py,
app/api/routers/billing.py): a payment provider's webhook becomes credits exactly once, or a visible refusal.

Every guarantee here is database behaviour, so a fake would only echo back what was written (constitution VII:
reliance on a real constraint, lock or transaction is stated and tested at this tier, not assumed):

  * `PRIMARY KEY (provider, event_id)` makes five sequential and twenty concurrent deliveries of one event
    ONE grant, and the second concurrent delivery blocks on the first's transaction instead of racing it;
  * the grant and the inbox status are ONE transaction, so there is no state "granted but not marked applied";
  * `UNIQUE (provider, customer_ref)` and the index on `credit_lots` stop a customer being re-pointed at another
    tenant and a payment being credited twice;
  * a trigger makes `applied` and `ignored` terminal, and what an event WAS immutable;
  * a refund after the credits were spent leaves a negative balance and the gate then refuses usage (spec D6).

The shared adapter contract (tests/billing/contract.py) is run here too, for EVERY registered adapter, through the
real inbox and wallet: "a replayed payload is applied once" is a claim about a database, not a parser.

Each test uses its own tenant, customer, product and event ids: the container is shared across tests and workers.
"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from app.agent import budget_holds, budgets
from app.api.routers import billing as billing_router
from app.billing import credits, inbox, store, webhooks
from app.billing.providers.base import BillingEvent, CreditProduct, EventKind
from app.core import metrics
from tests.billing.contract import HARNESSES, Delivery
from tests.conftest import metric_value
from tests.containers import ensure_postgres
from tests.integration.schema_reapply import reapply

pytestmark = pytest.mark.integration

D = Decimal
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

    for module in (webhooks, credits, inbox, budget_holds):
        monkeypatch.setattr(module, "get_connection", get_connection)
    return get_connection


@pytest.fixture(params=sorted(HARNESSES))
def harness(request, monkeypatch):
    instance = HARNESSES[request.param]()
    monkeypatch.setattr(billing_router, "_providers", {instance.name: instance.provider})
    return instance


class World:
    """One test's tenant, customer, product and payment: unique, so tests never touch each other's rows."""

    def __init__(self, url: str, provider: str = "fake"):
        self.url = url
        # The provider whose deliveries this test plays: the harness's, when the test uses one (so the same assertions hold for every
        # registered adapter), else "fake", which is only a label for tests that hand `process_event` a hand-built event.
        self.provider = provider
        tag = uuid.uuid4().hex[:10]
        self.tenant, self.customer, self.product, self.payment = f"t-{tag}", f"cus_{tag}", f"pack_{tag}", f"pay_{tag}"

    def event_id(self, label: str = "e") -> str:
        return f"evt_{label}_{uuid.uuid4().hex[:8]}"

    async def connect(self):
        return await psycopg.AsyncConnection.connect(self.url)

    async def link(self, tenant: str | None = None, customer: str | None = None, provider: str | None = None) -> None:
        async with await self.connect() as conn:
            await store.link_customer(conn, tenant or self.tenant, provider or self.provider, customer or self.customer)

    async def stock(self, credits_: str = "100", *, expires_after_days: int | None = None, active: bool = True, provider: str | None = None) -> None:
        async with await self.connect() as conn:
            await store.put_product(conn, CreditProduct(provider or self.provider, self.product, D(credits_), expires_after_days, active))

    async def one(self, sql: str, *params):
        async with await self.connect() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def scalar(self, sql: str, *params):
        return (await self.one(sql, *params))[0][0]

    async def inbox_row(self, provider: str, event_id: str):
        rows = await self.one(
            "SELECT status, tenant, error_class, attempts, payload FROM billing_webhook_events WHERE provider = %s AND event_id = %s",
            provider, event_id,
        )
        return rows[0] if rows else None

    async def lots(self) -> list[tuple]:
        return await self.one(
            "SELECT granted, source, provider, external_ref FROM credit_lots WHERE tenant = %s AND source <> 'overdraft' ORDER BY created_at",
            self.tenant,
        )

    async def balance(self) -> credits.Balance | None:
        return await credits.account_balance(self.tenant)


@pytest.fixture
def world(appdata_url, request) -> World:
    provider = request.getfixturevalue("harness").name if "harness" in request.fixturenames else "fake"
    return World(appdata_url, provider)


async def deliver(harness, delivery: Delivery) -> list[str]:
    """The delivery through the adapter's own parser and the real inbox: what the endpoint does, minus HTTP."""
    events = harness.provider.parse_webhook(delivery.headers, delivery.body)
    return [await webhooks.process_event(event) for event in events]


async def setup_deliver(harness, delivery: Delivery) -> list[str]:
    """A delivery made to set the stage for what a test is about, not to test: it must not have FAILED.

    `webhooks.process_event` never raises: a failure (a lock timeout, a deadlock with another worker's DDL, a database blip)
    is recorded and returned as "failed". A bare `await deliver(...)` throws that away, and the test then fails somewhere else
    with a message about something unrelated, as `test_the_database_itself_refuses_a_second_grant_of_one_payment` did
    ("DID NOT RAISE": the first grant had never happened, so the second one succeeded). Asserting here makes the failure say
    what it is, and where."""
    outcomes = await deliver(harness, delivery)
    assert "failed" not in outcomes, f"the setup delivery FAILED ({outcomes}); the reason is on its billing_webhook_events row"
    return outcomes


def count(outcome: str, provider: str) -> float:
    return metric_value(metrics.agent_billing_webhook_total, provider=provider, outcome=outcome)


class TestOneDeliveryIsOneGrant:
    async def test_a_purchase_grants_the_catalogs_credits_to_the_linked_tenant_not_what_the_payload_says(self, world, harness):
        """The payload claims a different tenant and a billion credits. It has nowhere to put the first, and the
        second is never read: who is the link's, how much is the catalog's (FR-014, FR-015)."""
        await world.link()
        await world.stock("100")
        applied_before = count("applied", world.provider)

        outcomes = await deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment, claims_tenant="evil-corp"))

        assert outcomes == ["applied"] and count("applied", world.provider) == applied_before + 1
        assert await world.lots() == [(D("100.000000"), "purchase", world.provider, world.payment)]
        assert (await world.balance()).available == D("100.000000")
        assert await world.scalar("SELECT count(*) FROM credit_accounts WHERE tenant = %s", "evil-corp") == 0

    async def test_the_inbox_row_is_applied_and_records_the_linked_tenant(self, world, harness):
        await world.link()
        await world.stock()
        event_id = world.event_id()

        await setup_deliver(harness, harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment))

        status, tenant, reason, attempts, _ = await world.inbox_row(world.provider, event_id)
        assert (status, tenant, reason, attempts) == ("applied", world.tenant, None, 1)

    async def test_the_same_delivery_five_times_is_one_grant(self, world, harness):
        await world.link()
        await world.stock("100")
        delivery = harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment)
        duplicates_before = count("duplicate", world.provider)

        outcomes = [await deliver(harness, delivery) for _ in range(5)]

        assert outcomes == [["applied"], ["duplicate"], ["duplicate"], ["duplicate"], ["duplicate"]]
        assert count("duplicate", world.provider) == duplicates_before + 4
        assert len(await world.lots()) == 1
        assert (await world.balance()).available == D("100.000000")

    async def test_twenty_concurrent_deliveries_of_one_event_are_one_grant(self, world, harness):
        """The race two retrying provider workers create: the second INSERT blocks on the first's transaction and
        then finds a finished row, rather than both granting."""
        await world.link()
        await world.stock("100")
        delivery = harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment)

        results = await asyncio.gather(*[deliver(harness, delivery) for _ in range(20)])

        flat = [outcome for outcomes in results for outcome in outcomes]
        assert flat.count("applied") == 1 and flat.count("duplicate") == 19
        assert len(await world.lots()) == 1 and (await world.balance()).available == D("100.000000")

    async def test_a_redelivery_after_the_inbox_row_was_swept_still_cannot_grant_twice(self, world, harness):
        """Retention may delete a finished row. The grant's own key, kept for good in the wallet, is what stops a
        late redelivery, so a sweep is safe."""
        await world.link()
        await world.stock("100")
        event_id = world.event_id()
        delivery = harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment)
        await setup_deliver(harness, delivery)
        async with await world.connect() as conn:
            await conn.execute("DELETE FROM billing_webhook_events WHERE provider = %s AND event_id = %s", (world.provider, event_id))

        outcomes = await deliver(harness, delivery)

        assert outcomes == ["ignored"], "the existing grant of that payment is found, so nothing is granted again"
        assert len(await world.lots()) == 1 and (await world.balance()).available == D("100.000000")

    async def test_a_second_event_for_the_same_payment_is_ignored(self, world, harness):
        """A provider can describe one payment in several events with different ids."""
        await world.link()
        await world.stock("100")
        for label in ("first", "second"):
            await setup_deliver(harness, harness.purchase(world.event_id(label), customer=world.customer, product=world.product, payment=world.payment))

        assert len(await world.lots()) == 1 and (await world.balance()).available == D("100.000000")

    async def test_two_events_for_one_payment_racing_still_credit_it_once(self, world, harness):
        """Both pass the 'already granted?' check at the same instant; the unique index decides. The loser fails
        loudly (it is retried), never credits the customer twice."""
        await world.link()
        await world.stock("100")
        deliveries = [harness.purchase(world.event_id(label), customer=world.customer, product=world.product, payment=world.payment) for label in "ab"]

        results = await asyncio.gather(*[deliver(harness, d) for d in deliveries])
        outcomes = [o for r in results for o in r]
        for delivery, outcome in zip(deliveries, outcomes, strict=True):
            if outcome == "failed":
                assert await deliver(harness, delivery) == ["ignored"], "its retry finds the payment already granted"

        assert len(await world.lots()) == 1 and (await world.balance()).available == D("100.000000")


class TestAForgeryStoresNothing:
    def client(self):
        app = FastAPI()
        app.include_router(billing_router.router)
        return TestClient(app)

    async def test_an_invalid_signature_is_rejected_counted_and_leaves_nothing_behind(self, world, harness):
        await world.link()
        await world.stock("100")
        event_id = world.event_id()
        good = harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment)
        forged = harness.forged(good)
        before = count("invalid_signature", world.provider)

        response = self.client().post(f"/billing/webhooks/{harness.name}", content=forged.body, headers=forged.headers)

        assert response.status_code == 400
        assert count("invalid_signature", world.provider) == before + 1
        assert await world.inbox_row(world.provider, event_id) is None, "a forger does not get to fill the inbox"
        assert await world.balance() is None, "and no wallet was opened"

    async def test_a_genuine_delivery_through_http_becomes_credits(self, world, harness):
        """The whole path: HTTP -> signature -> inbox -> link -> catalog -> wallet -> 200."""
        await world.link()
        await world.stock("250")
        delivery = harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment)

        response = self.client().post(f"/billing/webhooks/{harness.name}", content=delivery.body, headers=delivery.headers)

        assert response.status_code == 200
        assert (await world.balance()).available == D("250.000000")


class TestWhatCannotBeAppliedIsQuarantinedNotGuessedAt:
    async def test_an_unlinked_customer_is_quarantined_alerted_and_grants_nothing(self, world, harness):
        await world.stock("100")  # a real product, but nobody has linked this customer
        event_id = world.event_id()
        before = count("quarantined", world.provider)

        outcomes = await deliver(harness, harness.purchase(event_id, customer="cus_nobody_linked", product=world.product, payment=world.payment))

        assert outcomes == ["quarantined"] and count("quarantined", world.provider) == before + 1, "this is what pages (BillingWebhookQuarantined)"
        status, tenant, reason, _, _ = await world.inbox_row(world.provider, event_id)
        assert (status, tenant, reason) == ("quarantined", None, "unlinked_customer")
        assert await world.lots() == [] and await world.balance() is None

    async def test_through_http_a_quarantined_event_is_acknowledged_so_the_provider_stops_retrying(self, world, harness):
        await world.stock("100")
        delivery = harness.purchase(world.event_id(), customer="cus_nobody_linked", product=world.product, payment=world.payment)
        app = FastAPI()
        app.include_router(billing_router.router)

        response = TestClient(app).post(f"/billing/webhooks/{harness.name}", content=delivery.body, headers=delivery.headers)

        assert response.status_code == 200

    async def test_once_the_customer_is_linked_an_operator_can_reopen_the_event_and_it_applies(self, world, harness):
        """The recovery path the alert's runbook describes: link, set the row back to received, let the provider's
        redelivery (or a manual one) apply it. `quarantined` is the one terminal-looking state that may be reopened."""
        await world.stock("100")
        event_id = world.event_id()
        delivery = harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment)
        assert await deliver(harness, delivery) == ["quarantined"]
        assert await deliver(harness, delivery) == ["duplicate"], "unchanged until a person acts"
        await world.link()
        async with await world.connect() as conn:
            await conn.execute("UPDATE billing_webhook_events SET status = 'received' WHERE provider = %s AND event_id = %s", (world.provider, event_id))

        assert await deliver(harness, delivery) == ["applied"]
        assert (await world.balance()).available == D("100.000000")

    @pytest.mark.parametrize(
        "kind,product,expected",
        [
            (EventKind.CREDITS_PURCHASED, None, "unknown_product"),
            (EventKind.DISPUTE_OPENED, "ok", "dispute_needs_a_decision"),
            (EventKind.DISPUTE_CLOSED, "ok", "dispute_needs_a_decision"),
            (EventKind.PAYMENT_PARTIALLY_REFUNDED, "ok", "partial_refund_needs_a_decision"),
            (EventKind.SUBSCRIPTION_PERIOD_STARTED, "ok", "subscription_without_expiry"),
        ],
    )
    async def test_each_unhandled_case_is_quarantined_with_its_reason(self, world, kind, product, expected):
        await world.link()
        await world.stock("100")  # a catalog entry that never expires
        event = BillingEvent(
            world.provider, world.event_id(), kind, world.customer, world.product if product else "pack_not_in_the_catalog",
            world.payment, 1000, "usd", None, "x",
        )

        outcome = await webhooks.process_event(event)

        assert outcome == "quarantined"
        assert (await world.inbox_row(world.provider, event.event_id))[2] == expected
        assert await world.lots() == []

    async def test_a_retired_product_is_quarantined(self, world, harness):
        await world.link()
        await world.stock("100", active=False)

        outcomes = await deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment))

        assert outcomes == ["quarantined"]

    async def test_a_subscription_period_grants_an_expiring_lot(self, world):
        await world.link()
        await world.stock("500", expires_after_days=31)
        event = BillingEvent(world.provider, world.event_id(), EventKind.SUBSCRIPTION_PERIOD_STARTED, world.customer, world.product, world.payment, 2000, "usd", None, "x")

        assert await webhooks.process_event(event) == "applied"

        ((granted, source, _, _),) = await world.lots()
        assert (granted, source) == (D("500.000000"), "subscription")
        expires = await world.scalar("SELECT expires_at FROM credit_lots WHERE tenant = %s AND source = 'subscription'", world.tenant)
        assert timedelta(days=30) < expires - datetime.now(UTC) <= timedelta(days=31)

    async def test_an_unknown_event_type_is_recorded_as_ignored_with_what_it_was(self, world, harness):
        event_id = world.event_id()
        delivery = harness.unknown_event_type(event_id)
        (parsed,) = harness.provider.parse_webhook(delivery.headers, delivery.body)  # what THIS adapter calls the type it was sent

        outcomes = await deliver(harness, delivery)

        assert outcomes == ["ignored"] and parsed.raw_type
        status, _, reason, _, payload = await world.inbox_row(world.provider, event_id)
        assert (status, reason) == ("ignored", "unhandled_type") and payload["raw_type"] == parsed.raw_type


class TestARefund:
    async def bought(self, world, harness, credits_: str = "100"):
        await world.link()
        await world.stock(credits_)
        await setup_deliver(harness, harness.purchase(world.event_id("buy"), customer=world.customer, product=world.product, payment=world.payment))

    async def test_a_refund_before_any_spend_takes_the_credits_back(self, world, harness):
        await self.bought(world, harness)

        outcomes = await deliver(harness, harness.refund(world.event_id("refund"), customer=world.customer, payment=world.payment))

        assert outcomes == ["applied"]
        balance = await world.balance()
        assert (balance.available, balance.debt) == (D("0.000000"), D("0.000000"))
        assert await credits.verify(world.tenant) == []

    async def test_a_refund_after_the_credits_were_spent_leaves_debt_and_the_gate_then_refuses_usage(self, world, harness):
        """Spec D6 and US4's last scenario: the balance goes negative and new usage is refused until repaid."""
        await self.bought(world, harness, "100")
        await credits.debit(world.tenant, "70", idempotency_key=f"spent-{world.payment}")
        overdraft_before = metric_value(metrics.agent_credit_overdraft_total)

        outcomes = await deliver(harness, harness.refund(world.event_id("refund"), customer=world.customer, payment=world.payment))

        assert outcomes == ["applied"]
        balance = await world.balance()
        assert (balance.available, balance.debt) == (D("-70.000000"), D("70.000000"))
        gate = await budgets.check_allowance({"tenant": world.tenant, "principal": "p", "claims": {}}, limits=[], fail_policy="open", credit_gate=GATE)
        assert gate.status == "insufficient_credits", "usage is refused while the tenant is in debt"
        assert metric_value(metrics.agent_credit_overdraft_total) == overdraft_before, "a refund after spend is the policy, not a wallet running dry"
        assert await credits.verify(world.tenant) == []

    async def test_a_new_purchase_repays_the_debt_and_usage_resumes(self, world, harness):
        await self.bought(world, harness, "100")
        await credits.debit(world.tenant, "70", idempotency_key=f"spent-{world.payment}")
        await setup_deliver(harness, harness.refund(world.event_id("refund"), customer=world.customer, payment=world.payment))
        ctx = {"tenant": world.tenant, "principal": "p", "claims": {}}
        assert (await budgets.check_allowance(ctx, limits=[], fail_policy="open", credit_gate=GATE)).status == "insufficient_credits"

        await setup_deliver(harness, harness.purchase(world.event_id("again"), customer=world.customer, product=world.product, payment=f"{world.payment}-2"))

        assert (await world.balance()).available == D("30.000000")
        assert (await budgets.check_allowance(ctx, limits=[], fail_policy="open", credit_gate=GATE)).status == "ok"

    async def test_a_refund_delivered_twice_takes_the_credits_back_once(self, world, harness):
        await self.bought(world, harness)
        refund = harness.refund(world.event_id("refund"), customer=world.customer, payment=world.payment)

        outcomes = [await deliver(harness, refund) for _ in range(3)]

        assert outcomes == [["applied"], ["duplicate"], ["duplicate"]]
        assert (await world.balance()).available == D("0.000000")

    async def test_a_second_refund_event_for_the_same_payment_is_ignored_not_taken_twice(self, world, harness):
        await self.bought(world, harness)
        await setup_deliver(harness, harness.refund(world.event_id("r1"), customer=world.customer, payment=world.payment))

        outcomes = await deliver(harness, harness.refund(world.event_id("r2"), customer=world.customer, payment=world.payment))

        assert outcomes == ["ignored"]
        assert (await world.balance()).available == D("0.000000"), "a payment is taken back at most once"

    async def test_a_refund_before_its_purchase_is_held_then_applies_once_the_purchase_has(self, world, harness):
        await world.link()
        await world.stock("100")
        refund_id = world.event_id("refund")
        refund = harness.refund(refund_id, customer=world.customer, payment=world.payment)
        retry_before = count("retry", world.provider)

        assert await deliver(harness, refund) == ["retry"], "the provider is told 5xx and redelivers"
        assert count("retry", world.provider) == retry_before + 1
        status, _, reason, _, _ = await world.inbox_row(world.provider, refund_id)
        assert (status, reason) == ("received", "purchase_not_applied_yet")

        await setup_deliver(harness, harness.purchase(world.event_id("buy"), customer=world.customer, product=world.product, payment=world.payment))
        assert await deliver(harness, refund) == ["applied"]
        assert (await world.balance()).available == D("0.000000")

    async def test_a_held_refund_over_http_is_a_503(self, world, harness):
        await world.link()
        refund = harness.refund(world.event_id("refund"), customer=world.customer, payment=world.payment)
        app = FastAPI()
        app.include_router(billing_router.router)

        assert TestClient(app).post(f"/billing/webhooks/{harness.name}", content=refund.body, headers=refund.headers).status_code == 503

    async def test_a_refund_whose_purchase_never_arrives_is_quarantined_after_its_deadline(self, world, harness, monkeypatch):
        await world.link()
        refund_id = world.event_id("refund")
        refund = harness.refund(refund_id, customer=world.customer, payment=world.payment)
        assert await deliver(harness, refund) == ["retry"]
        monkeypatch.setattr(webhooks, "BILLING_REFUND_HOLD_HOURS", 0)  # the deadline has passed

        assert await deliver(harness, refund) == ["quarantined"]
        assert (await world.inbox_row(world.provider, refund_id))[2] == "purchase_never_applied"

    async def test_a_refund_never_reaches_another_tenants_grant_of_the_same_payment_reference(self, world, harness, appdata_url):
        """Payment references are the provider's, so two tenants can in principle share one. The lookup is by the
        LINKED tenant, so a refund can only ever take back its own tenant's grant."""
        other = World(appdata_url, world.provider)
        await world.link()
        await other.link()
        await world.stock("100")
        await other.stock("100")
        await setup_deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment="pay_shared"))
        await setup_deliver(harness, harness.purchase(other.event_id(), customer=other.customer, product=other.product, payment="pay_shared"))

        await setup_deliver(harness, harness.refund(world.event_id("refund"), customer=world.customer, payment="pay_shared"))

        assert (await world.balance()).available == D("0.000000")
        assert (await other.balance()).available == D("100.000000"), "the other tenant is untouched"

    async def test_credits_that_already_expired_are_not_taken_back_a_second_time(self, world, harness):
        """The customer lost those credits by expiry. Taking them back again would book debt for credits they
        no longer have. What WAS spent is still reclaimed (that is the refund policy)."""
        await world.link()
        async with await world.connect() as conn:
            await credits.grant_in(
                conn, world.tenant, "100", source="purchase", idempotency_key=f"g-{world.payment}", actor="test",
                provider=world.provider, external_ref=world.payment, expires_at=datetime.now(UTC) + timedelta(seconds=2),
            )
        await credits.debit(world.tenant, "30", idempotency_key=f"spent-{world.payment}")
        await asyncio.sleep(2.2)
        assert await credits.expire_due(tenant=world.tenant) == 1  # the 70 unspent credits expire

        assert await deliver(harness, harness.refund(world.event_id("refund"), customer=world.customer, payment=world.payment)) == ["applied"]

        balance = await world.balance()
        assert (balance.available, balance.debt) == (D("-30.000000"), D("30.000000")), "only the 30 that were spent"

    async def test_credits_expired_and_not_yet_swept_are_not_taken_back_either(self, world, harness):
        await world.link()
        async with await world.connect() as conn:
            await credits.grant_in(
                conn, world.tenant, "100", source="purchase", idempotency_key=f"g-{world.payment}", actor="test",
                provider=world.provider, external_ref=world.payment, expires_at=datetime.now(UTC) + timedelta(seconds=2),
            )
        await asyncio.sleep(2.2)  # past expires_at, never swept

        refund_id = world.event_id("refund")

        assert await deliver(harness, harness.refund(refund_id, customer=world.customer, payment=world.payment)) == ["ignored"]
        assert (await world.inbox_row(world.provider, refund_id))[2] == "nothing_to_reclaim"


class TestAFailureIsAtomicAndBounded:
    @pytest.fixture
    def wallet_fault(self, monkeypatch):
        real = credits._move

        async def fail(*args, **kwargs):
            raise RuntimeError("password=hunter2 host=db.internal")

        class Fault:
            def break_(self):
                monkeypatch.setattr(credits, "_move", fail)

            def heal(self):
                monkeypatch.setattr(credits, "_move", real)

        return Fault()

    async def test_a_failure_part_way_leaves_no_grant_and_is_retried_to_success(self, world, harness, wallet_fault):
        await world.link()
        await world.stock("100")
        event_id = world.event_id()
        delivery = harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment)
        failed_before = count("failed", world.provider)
        wallet_fault.break_()

        assert await deliver(harness, delivery) == ["failed"]

        assert count("failed", world.provider) == failed_before + 1, "this is what pages (BillingWebhookFailing)"
        assert await world.lots() == [] and await world.balance() is None, "the grant rolled back with the status"
        status, _, reason, attempts, _ = await world.inbox_row(world.provider, event_id)
        assert (status, attempts) == ("failed", 1)
        assert reason == "RuntimeError", "a class name only: an exception's text can carry a host or a credential"

        wallet_fault.heal()
        assert await deliver(harness, delivery) == ["applied"]
        assert len(await world.lots()) == 1 and (await world.balance()).available == D("100.000000")

    async def test_a_status_update_that_fails_after_the_grant_takes_the_grant_with_it(self, world, harness, monkeypatch):
        """The window 'granted but not marked applied' must not exist: they are one transaction."""
        await world.link()
        await world.stock("100")

        async def fail(*args, **kwargs):
            raise RuntimeError("the status write fell over")

        real_finish = webhooks._finish
        monkeypatch.setattr(webhooks, "_finish", fail)
        assert await deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment)) == ["failed"]

        assert await world.lots() == [], "no grant without its status"
        monkeypatch.setattr(webhooks, "_finish", real_finish)

    async def test_an_event_that_keeps_failing_is_quarantined_after_the_attempt_limit(self, world, harness, wallet_fault, monkeypatch):
        await world.link()
        await world.stock("100")
        event_id = world.event_id()
        delivery = harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment)
        monkeypatch.setattr(webhooks, "BILLING_WEBHOOK_MAX_ATTEMPTS", 3)
        wallet_fault.break_()
        quarantined_before = count("quarantined", world.provider)

        outcomes = [(await deliver(harness, delivery))[0] for _ in range(3)]

        assert outcomes == ["failed", "failed", "quarantined"], "a poison event is not retried for ever"
        assert count("quarantined", world.provider) == quarantined_before + 1
        status, _, reason, attempts, _ = await world.inbox_row(world.provider, event_id)
        assert (status, attempts) == ("quarantined", 3) and reason.startswith("max_attempts")
        wallet_fault.heal()
        assert await deliver(harness, delivery) == ["duplicate"], "and it stays quarantined until a person acts"


class TestWhatIsStored:
    async def test_the_raw_body_is_never_stored_only_the_normalized_fields(self, world, harness):
        await world.link()
        await world.stock("100")
        event_id = world.event_id()

        await setup_deliver(harness, harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment, buyer_details=True, claims_tenant="evil-corp"))

        payload = (await world.inbox_row(world.provider, event_id))[4]
        text = json.dumps(payload)
        for secret in ("jane.doe@example.com", "Jane Doe", "Privet Drive", "4242", "evil-corp", "999999999"):
            assert secret not in text, f"{secret!r} reached the stored payload"
        assert set(payload) == {"provider", "event_id", "kind", "customer_ref", "product_ref", "payment_ref", "amount_minor", "currency", "occurred_at", "raw_type"}


class TestTheSchemaGuards:
    async def test_an_applied_event_is_terminal(self, world, harness):
        await world.link()
        await world.stock("100")
        event_id = world.event_id()
        await setup_deliver(harness, harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment))

        async with await world.connect() as conn:
            with pytest.raises(pg_errors.RaiseException, match="terminal"):
                await conn.execute("UPDATE billing_webhook_events SET status = 'received' WHERE provider = %s AND event_id = %s", (world.provider, event_id))

    async def test_what_an_event_was_never_changes(self, world, harness):
        await world.link()
        await world.stock("100")
        event_id = world.event_id()
        await setup_deliver(harness, harness.purchase(event_id, customer=world.customer, product=world.product, payment=world.payment))

        async with await world.connect() as conn:
            with pytest.raises(pg_errors.RaiseException, match="never changes"):
                await conn.execute("UPDATE billing_webhook_events SET payload = '{}'::jsonb WHERE provider = %s AND event_id = %s", (world.provider, event_id))

    async def test_a_customer_cannot_be_linked_to_a_second_tenant(self, world, appdata_url):
        await world.link()
        thief = World(appdata_url)

        with pytest.raises(ValueError, match="already linked to a different tenant"):
            await thief.link(tenant=thief.tenant, customer=world.customer)

        async with await world.connect() as conn:
            assert await store.tenant_for_customer(conn, world.provider, world.customer) == world.tenant, "the link did not move"

    async def test_relinking_the_same_customer_to_the_same_tenant_is_idempotent(self, world):
        await world.link()
        await world.link()

        assert await world.scalar("SELECT count(*) FROM billing_customers WHERE tenant = %s", world.tenant) == 1

    async def test_a_tenant_has_one_customer_per_provider(self, world):
        await world.link()

        with pytest.raises(pg_errors.UniqueViolation):
            await world.link(customer=f"{world.customer}-second")

    async def test_the_database_itself_refuses_a_second_grant_of_one_payment(self, world, harness):
        await world.link()
        await world.stock("100")
        await setup_deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment))

        async with await world.connect() as conn:
            with pytest.raises(pg_errors.UniqueViolation):
                await credits.grant_in(
                    conn, world.tenant, "100", source="purchase", idempotency_key="a-different-key", actor="bug",
                    provider=world.provider, external_ref=world.payment,
                )

    async def test_the_script_can_be_applied_twice(self, world):
        # Through the helper, not a bare `execute`: re-running this DDL while other workers write to the same tables used to
        # deadlock with them, and sometimes the other side was the one that failed (tests/integration/schema_reapply.py).
        await reapply(world.url, "22-billing.sql")  # no exception


class TestTheSetupHelper:
    async def test_a_setup_delivery_that_failed_says_so_instead_of_leaving_the_test_to_fail_elsewhere(self, world, harness, monkeypatch):
        async def failing(event):
            return "failed"

        monkeypatch.setattr(webhooks, "process_event", failing)

        with pytest.raises(AssertionError, match="setup delivery FAILED"):
            await setup_deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment))

    async def test_a_setup_delivery_that_was_merely_ignored_or_a_duplicate_is_not_a_failure(self, world, harness, monkeypatch):
        async def ignoring(event):
            return "ignored"

        monkeypatch.setattr(webhooks, "process_event", ignoring)

        assert await setup_deliver(harness, harness.purchase(world.event_id(), customer=world.customer, product=world.product, payment=world.payment)) == ["ignored"]


class TestRetention:
    async def old_row(self, world, status: str, days: int, event_id: str) -> None:
        async with await world.connect() as conn:
            await conn.execute(
                "INSERT INTO billing_webhook_events (provider, event_id, event_type, status, payload, received_at) "
                "VALUES ('fake', %s, 'x', %s, '{}'::jsonb, now() - make_interval(days => %s))",
                (event_id, status, days),
            )

    async def test_it_deletes_only_old_finished_rows_and_never_an_open_question(self, world):
        ids = {status: world.event_id(status) for status in ("applied", "ignored", "quarantined", "received", "failed")}
        for status, event_id in ids.items():
            await self.old_row(world, status, 500, event_id)
        recent = world.event_id("recent")
        await self.old_row(world, "applied", 5, recent)

        await inbox.sweep_old_rows(older_than_days=400)

        survivors = {row[0] for row in await world.one("SELECT event_id FROM billing_webhook_events WHERE event_id = ANY(%s)", [*ids.values(), recent])}
        assert survivors == {ids["quarantined"], ids["received"], ids["failed"], recent}

    async def test_a_value_below_the_floor_raises_before_anything_is_deleted(self, world):
        event_id = world.event_id()
        await self.old_row(world, "applied", 40, event_id)

        with pytest.raises(ValueError, match="floor"):
            await inbox.sweep_old_rows(older_than_days=7)

        assert await world.inbox_row(world.provider, event_id) is not None

    async def test_a_second_run_finds_nothing_of_ours_left_and_does_not_disturb_what_it_must_keep(self, world):
        """Idempotent. (Not asserted as 'deletes zero rows': the table is shared with other tests and workers, so another
        test's old row may legitimately be what a sweep finds. What is ours is what is checked.)"""
        old, recent = world.event_id("old"), world.event_id("recent")
        await self.old_row(world, "applied", 500, old)
        await self.old_row(world, "applied", 5, recent)

        await inbox.sweep_old_rows(older_than_days=400)
        await inbox.sweep_old_rows(older_than_days=400)

        assert await world.inbox_row(world.provider, old) is None
        assert await world.inbox_row(world.provider, recent) is not None
