"""The webhook policy as a pure function (app/billing/webhooks.py::decide): what happens to a verified event,
decided from the event and what the app already knows. No database, so every refusal reason has its own test.

What the database then does with a decision (one grant under duplicate and concurrent delivery, the status and
the money committing together, a clawback booking debt) is real-Postgres behaviour:
tests/integration/test_billing_webhooks_real_postgres.py.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing import store, webhooks
from app.billing.providers.base import BillingEvent, CreditProduct, EventKind

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
D = Decimal


def event(kind=EventKind.CREDITS_PURCHASED, **overrides) -> BillingEvent:
    fields = {
        "provider": "fake", "event_id": "evt_1", "kind": kind, "customer_ref": "cus_1", "product_ref": "pack_100",
        "payment_ref": "pay_1", "amount_minor": 1000, "currency": "usd", "occurred_at": NOW, "raw_type": "x",
    }
    fields.update(overrides)
    return BillingEvent(**fields)


PACK = CreditProduct("fake", "pack_100", D("100"))
PLAN = CreditProduct("fake", "plan_pro", D("500"), expires_after_days=31)
GRANTED = store.GrantedLot("lot-1", D("100"), D("100"))


def decide(ev, *, tenant="acme", product=PACK, lot=None, received_at=NOW, now=NOW, **kwargs):
    return webhooks.decide(ev, tenant=tenant, product=product, lot=lot, received_at=received_at, now=now, **kwargs)


class TestAPurchase:
    def test_a_linked_customer_buying_a_known_product_is_granted(self):
        assert decide(event()) == webhooks.Plan("grant")

    def test_a_subscription_period_with_an_expiring_plan_is_granted(self):
        assert decide(event(EventKind.SUBSCRIPTION_PERIOD_STARTED, product_ref="plan_pro"), product=PLAN).action == "grant"

    def test_a_subscription_whose_catalog_entry_never_expires_is_quarantined_not_mis_granted(self):
        """The wallet itself refuses a subscription lot with no expiry (credits.EXPIRY_REQUIRED)."""
        plan = decide(event(EventKind.SUBSCRIPTION_PERIOD_STARTED, product_ref="pack_100"), product=PACK)

        assert plan == webhooks.Plan("quarantine", "subscription_without_expiry")

    def test_an_unlinked_customer_is_quarantined_never_granted_to_a_default_tenant(self):
        assert decide(event(), tenant=None) == webhooks.Plan("quarantine", "unlinked_customer")

    def test_no_customer_at_all_is_quarantined(self):
        assert decide(event(customer_ref=None), tenant=None) == webhooks.Plan("quarantine", "no_customer")

    def test_a_product_the_catalog_does_not_know_is_quarantined(self):
        assert decide(event(), product=None) == webhooks.Plan("quarantine", "unknown_product")

    def test_a_retired_product_is_quarantined(self):
        retired = CreditProduct("fake", "pack_100", D("100"), active=False)

        assert decide(event(), product=retired) == webhooks.Plan("quarantine", "unknown_product")

    def test_a_purchase_with_no_payment_reference_is_quarantined_because_it_could_never_be_refunded(self):
        assert decide(event(payment_ref=None)) == webhooks.Plan("quarantine", "no_payment_ref")

    def test_a_second_event_for_a_payment_already_granted_is_ignored(self):
        """A provider can describe one payment in several events; the second must not grant again."""
        assert decide(event(event_id="evt_2"), lot=GRANTED) == webhooks.Plan("ignore", "payment_already_granted")

    def test_the_decision_never_reads_an_amount_from_the_event(self):
        """FR-015: the credits are the catalog's, whatever the payment said."""
        rich = event(amount_minor=10**9)
        poor = event(amount_minor=1)

        assert decide(rich) == decide(poor) == webhooks.Plan("grant")


class TestARefund:
    def test_a_refund_of_an_applied_purchase_claws_back(self):
        assert decide(event(EventKind.PAYMENT_REFUNDED), lot=GRANTED).action == "clawback"

    def test_a_refund_whose_purchase_has_not_arrived_is_held_so_the_provider_retries(self):
        plan = decide(event(EventKind.PAYMENT_REFUNDED), lot=None, received_at=NOW, now=NOW + timedelta(hours=1))

        assert plan == webhooks.Plan("hold", "purchase_not_applied_yet")

    def test_a_held_refund_is_quarantined_once_its_deadline_passes(self):
        """A wait on a counterparty needs a deadline: the purchase may never come."""
        plan = decide(event(EventKind.PAYMENT_REFUNDED), lot=None, received_at=NOW, now=NOW + timedelta(hours=25), hold=timedelta(hours=24))

        assert plan == webhooks.Plan("quarantine", "purchase_never_applied")

    def test_a_refund_of_credits_that_already_expired_has_nothing_to_take_back(self):
        gone = store.GrantedLot("lot-1", D("100"), D("0"))

        assert decide(event(EventKind.PAYMENT_REFUNDED), lot=gone) == webhooks.Plan("ignore", "nothing_to_reclaim")

    def test_an_unlinked_customers_refund_is_quarantined(self):
        assert decide(event(EventKind.PAYMENT_REFUNDED), tenant=None, lot=None) == webhooks.Plan("quarantine", "unlinked_customer")


class TestWhatTheAppHasNotDecidedHowToHandle:
    """Never guessed at, never silently kept: a person is told (the quarantine alert)."""

    @pytest.mark.parametrize("kind", [EventKind.DISPUTE_OPENED, EventKind.DISPUTE_CLOSED])
    def test_a_chargeback_is_quarantined(self, kind):
        assert decide(event(kind), lot=GRANTED) == webhooks.Plan("quarantine", "dispute_needs_a_decision")

    def test_a_partial_refund_is_quarantined_not_treated_as_a_full_one(self):
        assert decide(event(EventKind.PAYMENT_PARTIALLY_REFUNDED), lot=GRANTED) == webhooks.Plan(
            "quarantine", "partial_refund_needs_a_decision"
        )

    def test_a_type_the_app_does_not_act_on_is_recorded_and_ignored(self):
        assert decide(event(EventKind.IGNORED), tenant=None, product=None) == webhooks.Plan("ignore", "unhandled_type")

    def test_an_unknown_type_is_ignored_even_for_an_unlinked_customer(self):
        """Quarantining noise would bury the alert that matters."""
        assert decide(event(EventKind.IGNORED, customer_ref=None), tenant=None).action == "ignore"
