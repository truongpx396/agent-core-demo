"""The billing-provider adapter contract: ONE suite every adapter must pass unchanged
(specs/010-credit-billing-readiness/contracts/billing-provider-port.md, FR-018, SC-004).

An adapter holds no business rules, only wire formats, so what is checked here is what a wire format can get
wrong in ways that cost money: accepting a forgery, naming a tenant, trusting an amount, storing a buyer's
details, crashing on a type it has never heard of. A new provider is added by writing a `Harness` for it (how to
play that provider: how it signs, what its native purchase and refund look like) and registering the adapter;
`test_every_registered_adapter_has_a_harness` fails if one is registered without, so a provider cannot
skip this suite.

This module is imported by tests/billing/test_provider_contract.py (the pure half, which needs no database) and
tests/integration/test_billing_webhooks_real_postgres.py (the half that applies the same deliveries through the
real inbox and wallet). It is not collected by pytest itself.
"""
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

from app.billing.providers import FACTORIES
from app.billing.providers.base import BillingProvider
from app.billing.providers.fake import FakeProvider
from app.billing.providers.polar import PolarProvider
from app.billing.providers.stripe import StripeProvider
from tests.billing.fake_polar_api import FakePolarApi
from tests.billing.fake_stripe_api import FakeStripeApi

Headers = dict[str, str]
NOW = 1_760_000_000  # the harnesses' clock: a fixed instant, so a replay window is tested, not waited for


@dataclass(frozen=True)
class Delivery:
    headers: Headers
    body: bytes


class Harness(Protocol):
    """How to play one provider. Every method returns a correctly SIGNED delivery in the provider's own format."""

    name: str
    provider: BillingProvider
    replay_window_seconds: int | None  # None if the provider defines no replay window

    def purchase(
        self, event_id: str = ..., *, customer: str = ..., product: str = ..., payment: str = ...,
        claims_tenant: str | None = ..., buyer_details: bool = ..., age_seconds: int = ...,
    ) -> Delivery: ...

    def refund(self, event_id: str = ..., *, customer: str = ..., payment: str = ...) -> Delivery: ...

    def unknown_event_type(self, event_id: str = ...) -> Delivery: ...

    def signed_garbage(self) -> Delivery:
        """Authentic (correctly signed) but not in this provider's event format."""
        ...

    def forged(self, delivery: Delivery) -> Delivery:
        """The same body, signed by someone who does not hold the secret."""
        ...

    def received_ids(self) -> list[str]:
        """The usage event ids the provider has recorded so far, once each (only meaningful for USAGE_EXPORT)."""
        ...

    def checkout_requests(self) -> list[dict[str, str]]:
        """What reached the provider's checkout API, decoded, in order (only meaningful for CHECKOUT)."""
        ...

    def paid(self, session_ref: str, *, event_id: str = ...) -> Delivery:
        """The signed delivery the provider would send when the customer pays the session `create_checkout` returned (only
        meaningful for CHECKOUT): it must carry back what `create_checkout` put there."""
        ...


class FakeHarness:
    name = "fake"
    replay_window_seconds = 300

    def __init__(self):
        self.provider = FakeProvider("whsec_contract", clock=lambda: NOW)

    def _signed(self, payload: dict, *, age_seconds: int = 0) -> Delivery:
        headers, body = self.provider.sign(payload, at=NOW - age_seconds)
        return Delivery(headers, body)

    def purchase(
        self, event_id="evt_purchase_1", *, customer="cus_1", product="pack_100", payment="pay_1",
        claims_tenant=None, buyer_details=False, age_seconds=0,
    ) -> Delivery:
        payload: dict = {
            "id": event_id, "type": "purchase.completed", "customer": customer, "product": product,
            "payment": payment, "amount_minor": 1000, "currency": "usd", "created": NOW - age_seconds,
        }
        if claims_tenant:
            payload["tenant"] = claims_tenant  # a field the app must never act on
        payload["credits"] = 999_999_999  # nor an amount of credits the payload asserts
        if buyer_details:
            payload.update(
                buyer_email="jane.doe@example.com", buyer_name="Jane Doe", billing_address="1 Privet Drive",
                card_last4="4242", card_number="4242424242424242",
            )
        return self._signed(payload, age_seconds=age_seconds)

    def refund(self, event_id="evt_refund_1", *, customer="cus_1", payment="pay_1") -> Delivery:
        return self._signed({
            "id": event_id, "type": "payment.refunded", "customer": customer, "payment": payment,
            "amount_minor": 1000, "currency": "usd", "created": NOW,
        })

    def unknown_event_type(self, event_id="evt_unknown_1") -> Delivery:
        return self._signed({"id": event_id, "type": "invoice.something_new_in_2027", "created": NOW})

    def signed_garbage(self) -> Delivery:
        return self._signed({"not": "an event"})

    def received_ids(self) -> list[str]:
        return list(self.provider.received)

    def forged(self, delivery: Delivery) -> Delivery:
        # The same event, signed by someone who does not hold the secret (it re-serialises to the same bytes).
        headers, body = FakeProvider("not-the-secret", clock=lambda: NOW).sign(json.loads(delivery.body), at=NOW)
        assert body == delivery.body
        return Delivery(headers, body)


class StripeHarness:
    """Plays Stripe: its `Stripe-Signature` scheme, its event and object shapes, and (through `FakeStripeApi`) the checkout endpoint.

    The signing here is the documented scheme written out a second time, so it is only as independent of the adapter as I am of
    myself. The independence comes from `tests/billing/test_stripe_adapter.py`, whose known-answer headers were generated by
    Stripe's own library, and from the real-sandbox tier."""

    name = "stripe"
    replay_window_seconds = 300
    SECRET = "whsec_contract"

    def __init__(self):
        self.api = FakeStripeApi()
        self.provider = StripeProvider(self.SECRET, api_key=self.api.api_key, clock=lambda: NOW, transport=self.api.transport)

    def _signed(self, payload: dict, *, age_seconds: int = 0, secret: str | None = None) -> Delivery:
        body = json.dumps(payload, separators=(",", ":")).encode()
        stamp = NOW - age_seconds
        mac = hmac.new((secret or self.SECRET).encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
        return Delivery({"Stripe-Signature": f"t={stamp},v1={mac}"}, body)

    @staticmethod
    def _event(event_id: str, event_type: str, obj: dict, created: int) -> dict:
        return {
            "id": event_id, "object": "event", "api_version": "2025-01-27.acacia", "created": created, "livemode": False,
            "type": event_type, "data": {"object": obj}, "pending_webhooks": 1, "request": {"id": None, "idempotency_key": None},
        }

    def purchase(
        self, event_id="evt_purchase_1", *, customer="cus_1", product="pack_100", payment="pay_1",
        claims_tenant=None, buyer_details=False, age_seconds=0,
    ) -> Delivery:
        session = {
            "id": "cs_test_contract", "object": "checkout.session", "mode": "payment", "status": "complete", "payment_status": "paid",
            "customer": customer, "payment_intent": payment, "amount_total": 1000, "currency": "usd",
            "metadata": {"product_ref": product, "credits": "999999999"},  # an amount of credits the payload asserts
        }
        if claims_tenant:
            session["metadata"]["tenant"] = claims_tenant  # a field the app must never act on
        if buyer_details:
            session["customer_details"] = {
                "email": "jane.doe@example.com", "name": "Jane Doe", "phone": "+15555550100",
                "address": {"line1": "1 Privet Drive", "city": "Little Whinging", "country": "GB", "postal_code": "WD1 1AA"},
            }
            session["customer_email"] = "jane.doe@example.com"
            session["payment_method_details"] = {"card": {"last4": "4242", "brand": "visa"}}
        return self._signed(self._event(event_id, "checkout.session.completed", session, NOW - age_seconds), age_seconds=age_seconds)

    def refund(self, event_id="evt_refund_1", *, customer="cus_1", payment="pay_1") -> Delivery:
        charge = {
            "id": "ch_contract", "object": "charge", "amount": 1000, "amount_refunded": 1000, "refunded": True,
            "customer": customer, "payment_intent": payment, "currency": "usd",
        }
        return self._signed(self._event(event_id, "charge.refunded", charge, NOW))

    def unknown_event_type(self, event_id="evt_unknown_1") -> Delivery:
        return self._signed(self._event(event_id, "invoice.something_new_in_2027", {"id": "in_contract", "object": "invoice"}, NOW))

    def signed_garbage(self) -> Delivery:
        return self._signed({"not": "an event"})

    def forged(self, delivery: Delivery) -> Delivery:
        forged = self._signed(json.loads(delivery.body), secret="whsec_not_the_secret")
        assert forged.body == delivery.body
        return forged

    def received_ids(self) -> list[str]:
        return []  # Stripe here is prepaid packs only: no usage is ever sent (D16)

    def checkout_requests(self) -> list[dict[str, str]]:
        return list(self.api.forms)

    def paid(self, session_ref: str, *, event_id: str = "evt_paid_1") -> Delivery:
        """What Stripe sends when the customer pays that session: the session, completed, echoing what `create_checkout` set."""
        session = {**self.api.sessions[session_ref], "status": "complete", "payment_status": "paid", "payment_intent": "pi_paid_1", "amount_total": 1000, "currency": "usd"}
        return self._signed(self._event(event_id, "checkout.session.completed", session, NOW))


class PolarHarness:
    """Plays Polar: Standard Webhooks headers, its Order shape, and (through `FakePolarApi`) the checkout endpoints.

    As with Stripe, the signing here is the documented scheme written out a second time; the independence comes from
    `tests/billing/test_polar_adapter.py` (known-answer headers generated by the Standard Webhooks library and verified by Polar's own
    SDK) and from the real-sandbox tier. The event id is the `webhook-id` HEADER: a Polar body carries none."""

    name = "polar"
    replay_window_seconds = 300
    SECRET = "whsec_" + base64.b64encode(b"polar-contract-key-24b!").decode()

    def __init__(self):
        self.api = FakePolarApi()
        self.provider = PolarProvider(
            self.SECRET, access_token=self.api.token, environment="sandbox", clock=lambda: NOW, transport=self.api.transport
        )

    def _signed(self, event_id: str, payload: dict, *, age_seconds: int = 0, secret: str | None = None) -> Delivery:
        body = json.dumps(payload, separators=(",", ":")).encode()
        stamp = NOW - age_seconds
        key = base64.b64decode((secret or self.SECRET).removeprefix("whsec_"))
        mac = base64.b64encode(hmac.new(key, f"{event_id}.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
        return Delivery({"webhook-id": event_id, "webhook-timestamp": str(stamp), "webhook-signature": f"v1,{mac}"}, body)

    @staticmethod
    def _event(event_type: str, data: dict) -> dict:
        return {"type": event_type, "timestamp": "2025-10-09T08:53:20.000000Z", "api_version": "2026-10", "data": data}

    @staticmethod
    def _order(order: str, customer: str, product: str, **extra) -> dict:
        return {
            "id": order, "status": "paid", "paid": True, "billing_reason": "purchase", "customer_id": customer, "product_id": product,
            "checkout_id": "chk_contract", "total_amount": 1000, "net_amount": 1000, "refunded_amount": 0, "currency": "usd",
            "metadata": {"credits": "999999999"},  # an amount of credits the payload asserts
            **extra,
        }

    def purchase(
        self, event_id="evt_purchase_1", *, customer="cus_1", product="pack_100", payment="pay_1",
        claims_tenant=None, buyer_details=False, age_seconds=0,
    ) -> Delivery:
        order = self._order(payment, customer, product)
        if claims_tenant:
            order["metadata"]["tenant"] = claims_tenant  # a field the app must never act on
        if buyer_details:
            order["billing_name"] = "Jane Doe"
            order["billing_address"] = {"line1": "1 Privet Drive", "city": "Little Whinging", "country": "GB", "postal_code": "WD1 1AA"}
            order["customer"] = {"id": customer, "email": "jane.doe@example.com", "name": "Jane Doe", "external_id": "ext-jane"}
            order["description"] = "Card ending 4242"
        return self._signed(event_id, self._event("order.paid", order), age_seconds=age_seconds)

    def refund(self, event_id="evt_refund_1", *, customer="cus_1", payment="pay_1") -> Delivery:
        order = self._order(payment, customer, "pack_100", status="refunded", refunded_amount=1000)
        return self._signed(event_id, self._event("order.refunded", order))

    def unknown_event_type(self, event_id="evt_unknown_1") -> Delivery:
        return self._signed(event_id, self._event("benefit_grant.cycled_in_2027", {"id": "bg_contract"}))

    def signed_garbage(self) -> Delivery:
        return self._signed("evt_garbage_1", {"not": "an event"})

    def forged(self, delivery: Delivery) -> Delivery:
        other = "whsec_" + base64.b64encode(b"not-the-contract-key-24").decode()
        forged = self._signed(delivery.headers["webhook-id"], json.loads(delivery.body), secret=other)
        assert forged.body == delivery.body
        return forged

    def received_ids(self) -> list[str]:
        return []  # Polar here is prepaid packs only: no usage is ever sent (D16)

    def checkout_requests(self) -> list[dict]:
        return list(self.api.bodies)

    def paid(self, session_ref: str, *, event_id: str = "evt_paid_1") -> Delivery:
        """What Polar sends when the customer pays that session: the order, echoing the customer and product the session was made for."""
        session = self.api.sessions[session_ref]
        order = self._order("ord_paid_1", session["customer_id"], session["product_id"], checkout_id=session_ref)
        return self._signed(event_id, self._event("order.paid", order))


# One entry per registered adapter. Adding an adapter to `app.billing.providers.FACTORIES` without adding its
# harness here fails `test_every_registered_adapter_has_a_harness`.
HARNESSES: dict[str, type] = {"fake": FakeHarness, "stripe": StripeHarness, "polar": PolarHarness}


def registered_without_a_harness() -> set[str]:
    return set(FACTORIES) - set(HARNESSES)
