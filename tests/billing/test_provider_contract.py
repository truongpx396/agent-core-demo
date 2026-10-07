"""The adapter contract, pure half (tests/billing/contract.py): what every billing-provider adapter must do with a
delivery before the app trusts any of it. No database, no network: `parse_webhook` is pure by contract.

The other half, that the same deliveries are applied exactly once through the REAL inbox and wallet, is
tests/integration/test_billing_webhooks_real_postgres.py, parameterised over the same harnesses.
"""
import json
import socket

import pytest

from app.billing.providers.base import (
    BillingEvent,
    Capability,
    EventKind,
    InvalidPayload,
    InvalidSignature,
)
from tests.billing.contract import HARNESSES, Delivery, registered_without_a_harness


@pytest.fixture(params=sorted(HARNESSES))
def harness(request):
    return HARNESSES[request.param]()


def parse(harness, delivery: Delivery) -> list[BillingEvent]:
    return harness.provider.parse_webhook(delivery.headers, delivery.body)


def test_every_registered_adapter_has_a_harness():
    """An adapter that has not passed this suite cannot be registered: registering one means writing its harness."""
    assert registered_without_a_harness() == set(), "register an adapter only together with its contract harness"


def test_a_harness_plays_the_adapter_it_is_registered_under(harness):
    assert harness.provider.name == harness.name
    assert isinstance(harness.provider.capabilities, frozenset)
    assert all(isinstance(c, Capability) for c in harness.provider.capabilities)


class TestAValidDelivery:
    def test_normalizes_to_the_expected_event(self, harness):
        (event,) = parse(harness, harness.purchase("evt_a", customer="cus_9", product="pack_5", payment="pay_7"))

        assert event.provider == harness.name
        assert event.event_id == "evt_a", "the PROVIDER's event id is the inbox's dedupe key"
        assert event.kind is EventKind.CREDITS_PURCHASED
        assert (event.customer_ref, event.product_ref, event.payment_ref) == ("cus_9", "pack_5", "pay_7")

    def test_a_refund_names_the_payment_it_reverses(self, harness):
        (purchase,) = parse(harness, harness.purchase(payment="pay_1"))
        (refund,) = parse(harness, harness.refund(payment="pay_1"))

        assert refund.kind is EventKind.PAYMENT_REFUNDED
        assert refund.payment_ref == purchase.payment_ref, "this is how a refund finds the grant it takes back"

    def test_parsing_is_pure_it_needs_no_network(self, harness, monkeypatch):
        def no_network(*args, **kwargs):
            raise AssertionError("parse_webhook must not do I/O")

        monkeypatch.setattr(socket.socket, "connect", no_network)

        assert parse(harness, harness.purchase())


class TestAForgeryIsRefused:
    def test_a_body_altered_by_one_byte_is_refused(self, harness):
        good = harness.purchase()
        tampered = Delivery(good.headers, good.body[:-2] + bytes([good.body[-2] ^ 1]) + good.body[-1:])

        with pytest.raises(InvalidSignature):
            parse(harness, tampered)

    def test_a_delivery_signed_with_another_secret_is_refused(self, harness):
        with pytest.raises(InvalidSignature):
            parse(harness, harness.forged(harness.purchase()))

    def test_a_delivery_with_no_signature_is_refused(self, harness):
        with pytest.raises(InvalidSignature):
            harness.provider.parse_webhook({}, harness.purchase().body)

    def test_a_replayed_delivery_outside_the_providers_window_is_refused(self, harness):
        if harness.replay_window_seconds is None:
            pytest.skip("this provider defines no replay window")

        with pytest.raises(InvalidSignature):
            parse(harness, harness.purchase(age_seconds=harness.replay_window_seconds + 60))

    def test_the_refusal_says_nothing_about_why(self, harness):
        """What was wrong with a forgery is information for the forger."""
        with pytest.raises(InvalidSignature) as exc:
            harness.provider.parse_webhook({}, b"{}")

        assert not exc.value.args


class TestWhatTheAppMustNeverTrust:
    def test_a_payload_that_names_another_tenant_cannot_carry_it(self, harness):
        """`BillingEvent` has no tenant field: a claim has nowhere to live, which is stronger than a rule that
        says to ignore it. The only way to a tenant is the link the app wrote (billing_customers)."""
        (event,) = parse(harness, harness.purchase(claims_tenant="someone-elses-tenant"))

        assert not hasattr(event, "tenant")
        assert "someone-elses-tenant" not in json.dumps(event.stored())

    def test_credits_the_payload_asserts_are_not_carried(self, harness):
        (event,) = parse(harness, harness.purchase())

        assert not hasattr(event, "credits")
        assert "999999999" not in json.dumps(event.stored()), "credits come from the server-side catalog"

    def test_the_stored_form_holds_no_buyer_details(self, harness):
        """The inbox keeps only the normalized, whitelisted fields, never the raw body (constitution VI)."""
        (event,) = parse(harness, harness.purchase(buyer_details=True))

        stored = json.dumps(event.stored())
        for detail in ("jane.doe@example.com", "Jane Doe", "Privet Drive", "4242"):
            assert detail not in stored, f"{detail!r} reached the stored payload"

    def test_the_stored_form_is_a_closed_set_of_fields(self, harness):
        (event,) = parse(harness, harness.purchase(buyer_details=True))

        assert set(event.stored()) == {
            "provider", "event_id", "kind", "customer_ref", "product_ref", "payment_ref",
            "amount_minor", "currency", "occurred_at", "raw_type",
        }


class TestWhatItDoesNotUnderstand:
    def test_an_unknown_event_type_is_ignored_never_an_error(self, harness):
        """A provider adds event types all the time; refusing them would make it retry for days."""
        (event,) = parse(harness, harness.unknown_event_type())

        assert event.kind is EventKind.IGNORED
        assert event.raw_type, "the inbox row records what it was"

    def test_an_authentic_body_in_the_wrong_shape_is_an_invalid_payload_not_a_forgery_or_a_crash(self, harness):
        with pytest.raises(InvalidPayload):
            parse(harness, harness.signed_garbage())
