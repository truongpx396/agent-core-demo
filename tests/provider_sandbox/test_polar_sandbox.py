"""The Polar adapter against Polar's real SANDBOX (specs/010 T029b). Manual: `make test-provider-sandbox`, never CI.

The hermetic suite proves the adapter against Polar's documentation and Polar's own SDK (its signature vectors are verified by the SDK's
verifier; its payload fixtures come from `polar trigger --json`). This proves it against Polar itself, in three ways nothing else can:

  * a Checkout request the real API accepts, whose session has the shape `tests/billing/fake_polar_api.py` assumes, and the fact that
    shapes the whole checkout design: **Polar ignores an `Idempotency-Key`** (this file sends one twice and gets two sessions), which is why
    the adapter searches before it creates. The reconciliation is exercised against the real list endpoint;
  * a delivery SIGNED BY POLAR (via `polar listen`, whose secret the CLI prints) verifies with the adapter's code, and the same bytes are
    refused when altered, stale, in the future, under another secret, or under another signature version;
  * the events Polar's own `polar trigger` produces for every type the adapter handles or must ignore normalize as intended, a purchase
    and the refund that reverses it share one `payment_ref`, and a partial refund is not a full one.

What is NOT proved here, and is said so in the PR: a real PAID order. Paying a Polar sandbox checkout takes a browser (a Stripe Elements
card form), which this tier does not drive, so the purchase and refund events are Polar's SAMPLE events (a generator's payloads, signed by
Polar's real delivery path), not those of an order a buyer paid.

Needs: POLAR_ACCESS_TOKEN (an Organization Access Token from sandbox.polar.sh; `POLAR_ENVIRONMENT` must be `sandbox` or the run FAILS) and
the `polar` CLI logged in to the same organization. It creates labelled objects (`metadata.agent_core_demo_test=true`), archives the
product and deletes the customer afterwards; checkout sessions expire on their own.
"""
import os
import shutil
from decimal import Decimal

import httpx
import pytest

from app.billing.providers.base import (
    BillingCustomer,
    CheckoutError,
    CreditProduct,
    EventKind,
    InvalidSignature,
)
from app.billing.providers.polar import ATTEMPT_KEY, PolarProvider
from tests.provider_sandbox.conftest import (
    POLAR_SANDBOX_API,
    refuse_beside_a_foreign_listener,
)
from tests.provider_sandbox.polar_listener import PolarListener

pytestmark = pytest.mark.provider_sandbox

SUCCESS, CANCEL = "https://example.com/ok", "https://example.com/no"
SEED = 11  # one seed gives a purchase sample and a refund sample the same order id, as a real order's two events would share one


# --- the Checkout API, for real -----------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pack(polar_sandbox):
    return polar_sandbox.pack(500), polar_sandbox.customer()


def attempt(polar_token, pack, key: str, *, customer: dict | None = None, product_ref: str | None = None):
    product, buyer = pack
    provider = PolarProvider("unused-here", access_token=polar_token, environment="sandbox")
    return provider.create_checkout(
        BillingCustomer("acme-tenant", "polar", (customer or buyer)["id"]),
        CreditProduct("polar", product_ref or product["id"], Decimal(100)),
        idempotency_key=key, success_url=SUCCESS, cancel_url=CANCEL,
    )


def fresh(label: str) -> str:
    return f"sandbox-{label}-{os.urandom(6).hex()}"


class TestCheckoutAgainstTheRealApi:
    async def test_the_request_is_accepted_and_the_session_has_the_shape_the_fake_assumes(self, polar_token, polar_sandbox, pack):
        product, buyer = pack
        key = fresh("shape")

        session = await attempt(polar_token, pack, key)

        real = polar_sandbox.get(f"/checkouts/{session.session_ref}")
        assert session.url.startswith("https://sandbox.polar.sh/checkout/")
        assert real["status"] == "open" and real["customer_id"] == buyer["id"] and real["product_id"] == product["id"]
        assert real["metadata"] == {ATTEMPT_KEY: key}, "the attempt is remembered in the session, which is what the search finds it by"
        assert (real["total_amount"], real["currency"]) == (500, "usd"), "the amount is the product's price, never sent by the adapter"
        assert (real["success_url"], real["return_url"]) == (SUCCESS, CANCEL)

    async def test_polar_ignores_an_idempotency_key_which_is_why_the_adapter_searches_before_it_creates(self, polar_token, polar_sandbox, pack):
        """If this ever fails, Polar has added idempotency: the adapter's search is then redundant and its docs and fake are out of date."""
        product, buyer = pack
        body = {"products": [product["id"]], "customer_id": buyer["id"], "metadata": {ATTEMPT_KEY: fresh("raw")}}
        headers = {"Authorization": f"Bearer {polar_token}", "Idempotency-Key": fresh("header")}

        with httpx.Client(base_url=POLAR_SANDBOX_API, timeout=30) as client:
            first = client.post("/checkouts/", json=body, headers=headers)
            second = client.post("/checkouts/", json=body, headers=headers)

        assert (first.status_code, second.status_code) == (201, 201)
        assert first.json()["id"] != second.json()["id"]

    async def test_the_same_attempt_twice_is_one_session(self, polar_token, polar_sandbox, pack):
        key = fresh("replay")

        first = await attempt(polar_token, pack, key)
        again = await attempt(polar_token, pack, key)

        assert again == first

    async def test_a_different_attempt_is_a_different_session(self, polar_token, polar_sandbox, pack):
        first = await attempt(polar_token, pack, fresh("a"))
        second = await attempt(polar_token, pack, fresh("b"))

        assert second.session_ref != first.session_ref

    async def test_the_search_is_per_customer_so_one_buyer_is_never_handed_anothers_session(self, polar_token, polar_sandbox, pack):
        key = fresh("scoped")
        other = polar_sandbox.customer()

        mine = await attempt(polar_token, pack, key)
        theirs = await attempt(polar_token, pack, key, customer=other)

        assert theirs.session_ref != mine.session_ref

    async def test_a_wrong_token_is_refused_without_a_retry_and_its_text_is_not_repeated(self, pack):
        product, buyer = pack
        provider = PolarProvider("unused-here", access_token="polar_oat_" + "x" * 40, environment="sandbox")

        with pytest.raises(CheckoutError) as caught:
            await provider.create_checkout(
                BillingCustomer("t", "polar", buyer["id"]), CreditProduct("polar", product["id"], Decimal(1)),
                idempotency_key=fresh("badtoken"), success_url=SUCCESS, cancel_url=CANCEL,
            )

        assert caught.value.status == 401 and caught.value.retryable is False
        assert "xxxx" not in str(caught.value), "Polar's reply can quote part of the token; the message must not"

    async def test_a_product_id_that_is_not_one_names_the_field_and_not_polars_words(self, polar_token, pack):
        with pytest.raises(CheckoutError) as caught:
            await attempt(polar_token, pack, fresh("noproduct"), product_ref="not-a-product-id")

        assert caught.value.status == 422 and caught.value.retryable is False
        assert "field query.product_id" in str(caught.value) and "not-a-product-id" not in str(caught.value)


# --- real signed deliveries ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def listener(polar_token):
    if shutil.which("polar") is None:
        pytest.skip("the `polar` CLI is not installed (curl -fsSL https://polar.sh/install.sh | bash, then `polar auth login`)")
    refuse_beside_a_foreign_listener("polar")
    live = PolarListener()
    try:
        live.start()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    yield live
    live.stop()


def send(listener: PolarListener, event: str, *overrides: str, seed: int | None = None):
    """Ask Polar to send a sample and wait for it. The CLI's own exit status is not asserted: it reports a failure for a delivery that
    arrived whenever the receiver's connection handling differs from what it expects, so what counts is what was received."""
    before = len(listener.deliveries())
    listener.trigger(event, *overrides, seed=seed)
    return listener.wait_for(lambda e: e.get("type") == event, after=before)


def parse(listener: PolarListener, delivery, **kwargs):
    return PolarProvider(listener.secret, access_token="unused", environment="sandbox", **kwargs).parse_webhook(delivery.headers, delivery.body)


def stamp_of(delivery) -> int:
    return int(delivery.headers["webhook-timestamp"])


PURCHASE = ("data.billing_reason=purchase",)


@pytest.fixture(scope="module")
def purchase(listener):
    return send(listener, "order.paid", *PURCHASE, seed=SEED)


class TestARealSignedDelivery:
    def test_it_verifies_with_the_secret_the_cli_printed(self, listener, purchase):
        (event,) = parse(listener, purchase)

        assert event.raw_type == "order.paid" and event.event_id == purchase.headers["webhook-id"]

    def test_the_event_id_is_the_header_because_the_body_has_none(self, purchase):
        assert not [key for key in purchase.event if "id" in key.lower().split("_")], "a Polar body carries no event id"
        assert purchase.headers["webhook-id"]

    def test_every_delivery_has_its_own_event_id(self, listener, purchase):
        other = send(listener, "order.paid", *PURCHASE, seed=SEED)

        assert other.headers["webhook-id"] != purchase.headers["webhook-id"]

    def test_one_extra_byte_is_refused(self, listener, purchase):
        with pytest.raises(InvalidSignature):
            PolarProvider(listener.secret, access_token="x", environment="sandbox").parse_webhook(purchase.headers, purchase.body + b" ")

    def test_another_secret_is_refused(self, purchase):
        with pytest.raises(InvalidSignature):
            PolarProvider("not-the-cli-session-secret", access_token="x", environment="sandbox").parse_webhook(purchase.headers, purchase.body)

    def test_another_event_id_is_refused(self, listener, purchase):
        with pytest.raises(InvalidSignature):
            PolarProvider(listener.secret, access_token="x", environment="sandbox").parse_webhook(
                {**purchase.headers, "webhook-id": "msg_someone_elses"}, purchase.body
            )

    def test_the_signature_under_another_version_label_is_refused(self, listener, purchase):
        relabelled = {**purchase.headers, "webhook-signature": purchase.headers["webhook-signature"].replace("v1,", "v2,")}

        with pytest.raises(InvalidSignature):
            PolarProvider(listener.secret, access_token="x", environment="sandbox").parse_webhook(relabelled, purchase.body)

    def test_the_window_is_five_minutes_in_both_directions(self, listener, purchase):
        t = stamp_of(purchase)

        for edge in (t + 300, t - 300):
            assert parse(listener, purchase, clock=lambda edge=edge: edge)
        for beyond in (t + 301, t - 301):
            with pytest.raises(InvalidSignature):
                parse(listener, purchase, clock=lambda beyond=beyond: beyond)

    def test_a_real_delivery_arrives_within_the_window_on_this_machines_clock(self, purchase):
        """If this fails the machine's clock is badly off, and every real webhook would be refused for the same reason."""
        assert abs(purchase.received_at - stamp_of(purchase)) < 300


class TestRealEventsNormalize:
    def test_a_purchase_names_the_order_the_customer_the_product_and_what_was_paid(self, listener, purchase):
        (event,) = parse(listener, purchase)
        order = purchase.event["data"]

        assert event.kind is EventKind.CREDITS_PURCHASED
        assert (event.customer_ref, event.product_ref, event.payment_ref) == (order["customer_id"], order["product_id"], order["id"])
        assert (event.amount_minor, event.currency) == (order["total_amount"], order["currency"])

    def test_polars_default_sample_is_a_subscription_and_grants_nothing(self, listener):
        delivery = send(listener, "order.paid")

        (event,) = parse(listener, delivery)

        assert delivery.event["data"]["billing_reason"] == "subscription_create"
        assert event.kind is EventKind.IGNORED

    def test_a_full_refund_reverses_the_very_order_that_was_bought(self, listener, purchase):
        (bought,) = parse(listener, purchase)

        delivery = send(listener, "order.refunded", *PURCHASE, seed=SEED)
        (refunded,) = parse(listener, delivery)

        assert refunded.kind is EventKind.PAYMENT_REFUNDED
        assert refunded.payment_ref == bought.payment_ref, "the refund finds the grant it takes back (one seed, one order id)"
        assert refunded.amount_minor == bought.amount_minor

    def test_a_partial_refund_is_not_a_full_one(self, listener, purchase):
        delivery = send(
            listener, "order.refunded", *PURCHASE, "data.status=partially_refunded", "data.refunded_amount=300", seed=SEED
        )

        (event,) = parse(listener, delivery)
        (bought,) = parse(listener, purchase)

        assert (event.kind, event.amount_minor, event.payment_ref) == (EventKind.PAYMENT_PARTIALLY_REFUNDED, 300, bought.payment_ref)

    @pytest.mark.parametrize(
        "event_type",
        ["order.created", "order.updated", "refund.created", "refund.updated", "checkout.created", "checkout.updated", "checkout.expired",
         "customer.created", "subscription.created", "subscription.cycled", "product.updated"],
    )
    def test_every_other_event_polar_can_send_is_ignored_and_keeps_no_references(self, listener, event_type):
        delivery = send(listener, event_type)

        (event,) = parse(listener, delivery)

        assert event.kind is EventKind.IGNORED and event.raw_type == event_type
        assert (event.customer_ref, event.product_ref, event.payment_ref, event.amount_minor) == (None, None, None, None)

    def test_the_buyers_details_in_a_real_order_never_reach_the_stored_form(self, listener, purchase):
        (event,) = parse(listener, purchase)
        order = purchase.event["data"]
        stored = str(event.stored()).lower()

        for detail in (order["billing_name"], order["customer"]["email"]):
            assert detail and detail.lower() not in stored
