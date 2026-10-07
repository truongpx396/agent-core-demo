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
import json
from dataclasses import dataclass
from typing import Protocol

from app.billing.providers import FACTORIES
from app.billing.providers.base import BillingProvider
from app.billing.providers.fake import FakeProvider

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

    def forged(self, delivery: Delivery) -> Delivery:
        # The same event, signed by someone who does not hold the secret (it re-serialises to the same bytes).
        headers, body = FakeProvider("not-the-secret", clock=lambda: NOW).sign(json.loads(delivery.body), at=NOW)
        assert body == delivery.body
        return Delivery(headers, body)


# One entry per registered adapter. Adding an adapter to `app.billing.providers.FACTORIES` without adding its
# harness here fails `test_every_registered_adapter_has_a_harness`.
HARNESSES: dict[str, type] = {"fake": FakeHarness}


def registered_without_a_harness() -> set[str]:
    return set(FACTORIES) - set(HARNESSES)
