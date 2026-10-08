"""The Stripe adapter against Stripe's real SANDBOX (specs/010 T029a). Manual: `make test-provider-sandbox`, never CI.

The hermetic suite proves the adapter against Stripe's DOCUMENTATION: its signatures come from Stripe's own library, its payloads from the
API reference. This proves it against Stripe itself, in three ways nothing else can:

  * a Checkout request the real API accepts, and the session it returns has the shape `tests/billing/fake_stripe_api.py` assumes (this
    file asserts those very behaviours, which is what keeps that fake honest);
  * a delivery SIGNED BY STRIPE (via `stripe listen`, whose secret the CLI prints) verifies with the adapter's code, and the same bytes
    are refused when altered, stale, under another secret, or under only the `v0` label;
  * a REAL `checkout.session.completed` and the REAL `charge.refunded` it leads to normalize to a purchase and a refund that share one
    `payment_ref`: the one fact the wallet's refund logic cannot work without.

What is NOT proved here, and is said so in the PR: the sandbox cannot complete a hosted Checkout page without a browser, so the purchase
event comes from `stripe trigger checkout.session.completed`'s fixture (a guest checkout with no `customer` and no `metadata.product_ref`);
that this adapter ties a session this app created to a catalog entry rests on the open session's metadata (asserted below) plus Stripe's
documented behaviour of copying a session's metadata into its completion event.

Needs: STRIPE_API_KEY (a `sk_test_`/`rk_test_` key; a live key FAILS the run), the `stripe` CLI logged in to the same sandbox. It creates
labelled objects (`metadata[agent_core_demo_test]=true`) and archives/deletes what it can; PaymentIntents and Charges cannot be deleted.
"""
import os
import shutil
import subprocess
from decimal import Decimal

import pytest

from app.billing.providers.base import (
    BillingCustomer,
    CheckoutError,
    CreditProduct,
    EventKind,
    InvalidSignature,
)
from app.billing.providers.stripe import StripeProvider
from tests.provider_sandbox.stripe_listener import StripeListener, redact

pytestmark = pytest.mark.provider_sandbox

EVENTS = ("checkout.session.completed", "charge.refunded", "charge.dispute.created")
SUCCESS, CANCEL = "https://example.com/ok", "https://example.com/no"


def parse(listener: StripeListener, delivery, **kwargs):
    return StripeProvider(listener.secret, **kwargs).parse_webhook(delivery.headers, delivery.body)


def stamp_of(delivery) -> int:
    return int(dict(part.split("=", 1) for part in delivery.headers["stripe-signature"].split(","))["t"])


# --- the Checkout request, against the real API -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pack(stripe_sandbox):
    return stripe_sandbox.pack(500), stripe_sandbox.customer()


def start(stripe_key, stripe_sandbox, pack, key: str, **overrides):
    price, customer = pack
    provider = StripeProvider("whsec_unused_here", api_key=stripe_key)
    return provider.create_checkout(
        BillingCustomer("acme-tenant", "stripe", customer["id"]),
        CreditProduct("stripe", overrides.get("price", price["id"]), Decimal(100)),
        idempotency_key=key, success_url=overrides.get("success", SUCCESS), cancel_url=CANCEL,
    )


class TestCheckoutAgainstTheRealApi:
    async def test_the_request_is_accepted_and_the_session_has_the_shape_the_fake_assumes(self, stripe_key, stripe_sandbox, pack, request):
        price, customer = pack

        session = await start(stripe_key, stripe_sandbox, pack, f"sandbox-{request.node.name}-{os.urandom(4).hex()}")
        stripe_sandbox.later(lambda: stripe_sandbox.post(f"/v1/checkout/sessions/{session.session_ref}/expire"))

        real = stripe_sandbox.get(f"/v1/checkout/sessions/{session.session_ref}")
        assert session.url.startswith("https://checkout.stripe.com/") and session.session_ref.startswith("cs_test_")
        assert (real["mode"], real["status"], real["payment_status"]) == ("payment", "open", "unpaid")
        assert real["payment_intent"] is None, "null until the session completes: the adapter's payment_ref comes from the completion event"
        assert real["customer"] == customer["id"] and real["metadata"] == {"product_ref": price["id"]}
        assert (real["amount_total"], real["currency"]) == (500, "usd"), "the amount is the Price's, never sent by the adapter"

    async def test_the_same_key_returns_the_same_session(self, stripe_key, stripe_sandbox, pack):
        key = f"sandbox-replay-{os.urandom(6).hex()}"

        first = await start(stripe_key, stripe_sandbox, pack, key)
        again = await start(stripe_key, stripe_sandbox, pack, key)
        stripe_sandbox.later(lambda: stripe_sandbox.post(f"/v1/checkout/sessions/{first.session_ref}/expire"))

        assert again.session_ref == first.session_ref

    async def test_the_same_key_with_other_parameters_is_refused_not_a_second_purchase(self, stripe_key, stripe_sandbox, pack):
        key = f"sandbox-conflict-{os.urandom(6).hex()}"
        first = await start(stripe_key, stripe_sandbox, pack, key)
        stripe_sandbox.later(lambda: stripe_sandbox.post(f"/v1/checkout/sessions/{first.session_ref}/expire"))

        with pytest.raises(CheckoutError) as caught:
            await start(stripe_key, stripe_sandbox, pack, key, success="https://example.com/elsewhere")

        assert (caught.value.status, caught.value.code, caught.value.retryable) == (400, "idempotency_error", False)

    async def test_a_wrong_key_is_refused_and_not_worth_retrying(self, stripe_sandbox, pack):
        price, customer = pack
        provider = StripeProvider("whsec_unused_here", api_key="sk_test_" + "x" * 40)

        with pytest.raises(CheckoutError) as caught:
            await provider.create_checkout(
                BillingCustomer("t", "stripe", customer["id"]), CreditProduct("stripe", price["id"], Decimal(1)),
                idempotency_key=f"sandbox-badkey-{os.urandom(6).hex()}", success_url=SUCCESS, cancel_url=CANCEL,
            )

        assert caught.value.status == 401 and caught.value.retryable is False
        assert "xxxx" not in str(caught.value), "Stripe's reply quotes part of the key; the message must not"

    async def test_an_unknown_price_names_the_field_and_not_stripes_words(self, stripe_key, stripe_sandbox, pack):
        with pytest.raises(CheckoutError) as caught:
            await start(stripe_key, stripe_sandbox, pack, f"sandbox-noprice-{os.urandom(6).hex()}", price="price_does_not_exist")

        assert (caught.value.status, caught.value.code, caught.value.retryable) == (400, "resource_missing", False)
        assert "field line_items[0][price]" in str(caught.value) and "price_does_not_exist" not in str(caught.value)


# --- real signed deliveries ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def listener(stripe_key):
    if shutil.which("stripe") is None:
        pytest.skip("the `stripe` CLI is not installed (brew install stripe/stripe-cli/stripe, then `stripe login`)")
    live = StripeListener(stripe_key, EVENTS)
    try:
        live.start()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    yield live
    live.stop()


@pytest.fixture(scope="module")
def purchase(listener, stripe_key):
    """A REAL paid `checkout.session.completed`, signed by Stripe and delivered through the CLI. The fixture's shipping parameter is
    refused by Managed Payments (HTTP 400), so it is removed and Managed Payments is switched off for that one request."""
    before = len(listener.deliveries())
    result = subprocess.run(
        [
            "stripe", "trigger", "checkout.session.completed",
            "--override", "checkout_session:managed_payments[enabled]=false", "--remove", "checkout_session:payment_intent_data",
        ],
        env={**os.environ, "STRIPE_API_KEY": stripe_key}, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, redact(result.stdout + result.stderr)[-600:]
    return listener.wait_for(lambda e: e["type"] == "checkout.session.completed", after=before)


class TestARealSignedDelivery:
    def test_it_verifies_with_the_secret_the_cli_printed(self, listener, purchase):
        (event,) = parse(listener, purchase)

        assert event.raw_type == "checkout.session.completed" and event.event_id == purchase.event["id"]

    def test_it_carries_the_v0_label_the_adapter_is_built_to_ignore(self, purchase):
        schemes = {part.split("=", 1)[0] for part in purchase.headers["stripe-signature"].split(",")}

        assert {"t", "v1", "v0"} <= schemes, "if Stripe stops sending v0 on test events, the adapter's docs and tests say it still does"

    def test_the_v0_signature_alone_is_refused(self, listener, purchase):
        parts = [p for p in purchase.headers["stripe-signature"].split(",") if not p.startswith("v1=")]

        with pytest.raises(InvalidSignature):
            StripeProvider(listener.secret).parse_webhook({"stripe-signature": ",".join(parts)}, purchase.body)

    def test_the_v1_signature_under_the_v0_label_is_refused(self, listener, purchase):
        fields = dict(part.split("=", 1) for part in purchase.headers["stripe-signature"].split(","))

        with pytest.raises(InvalidSignature):
            StripeProvider(listener.secret).parse_webhook({"stripe-signature": f"t={fields['t']},v0={fields['v1']}"}, purchase.body)

    def test_one_extra_byte_is_refused(self, listener, purchase):
        with pytest.raises(InvalidSignature):
            StripeProvider(listener.secret).parse_webhook(purchase.headers, purchase.body + b" ")

    def test_another_secret_is_refused(self, purchase):
        with pytest.raises(InvalidSignature):
            StripeProvider("whsec_not_the_cli_session_secret").parse_webhook(purchase.headers, purchase.body)

    def test_it_is_refused_once_stale_and_accepted_at_the_edge(self, listener, purchase):
        t = stamp_of(purchase)

        assert parse(listener, purchase, clock=lambda: t + 300)
        with pytest.raises(InvalidSignature):
            parse(listener, purchase, clock=lambda: t + 301)

    def test_a_real_delivery_arrives_within_the_window_on_this_machines_clock(self, listener, purchase):
        """If this fails the machine's clock is badly off, and every real webhook would be refused for the same reason."""
        assert abs(purchase.received_at - stamp_of(purchase)) < 300


class TestARealPurchaseAndItsRefund:
    def test_the_purchase_names_the_payment_and_what_was_paid(self, listener, purchase):
        (event,) = parse(listener, purchase)
        session = purchase.event["data"]["object"]

        assert event.kind is EventKind.CREDITS_PURCHASED
        assert event.payment_ref == session["payment_intent"] and event.payment_ref.startswith("pi_")
        assert (event.amount_minor, event.currency) == (session["amount_total"], session["currency"])

    def test_a_full_refund_reverses_the_very_payment_that_was_bought(self, listener, purchase, stripe_sandbox):
        (bought,) = parse(listener, purchase)
        before = len(listener.deliveries())

        stripe_sandbox.post("/v1/refunds", {"payment_intent": bought.payment_ref, "metadata[agent_core_demo_test]": "true"})
        delivery = listener.wait_for(
            lambda e: e["type"] == "charge.refunded" and e["data"]["object"]["payment_intent"] == bought.payment_ref, after=before
        )
        (refunded,) = parse(listener, delivery)

        assert refunded.kind is EventKind.PAYMENT_REFUNDED
        assert refunded.payment_ref == bought.payment_ref, "the refund finds the grant it takes back"
        assert refunded.amount_minor == bought.amount_minor

    def test_a_partial_refund_is_not_a_full_one_and_the_rest_then_is(self, listener, stripe_sandbox):
        intent = stripe_sandbox.paid_intent(1000)
        assert intent["status"] == "succeeded", "control: the test card paid"
        before = len(listener.deliveries())

        stripe_sandbox.post("/v1/refunds", {"payment_intent": intent["id"], "amount": "300", "metadata[agent_core_demo_test]": "true"})
        partial = listener.wait_for(
            lambda e: e["type"] == "charge.refunded" and e["data"]["object"]["payment_intent"] == intent["id"], after=before
        )
        (first,) = parse(listener, partial)
        after_first = len(listener.deliveries())
        stripe_sandbox.post("/v1/refunds", {"payment_intent": intent["id"], "amount": "700", "metadata[agent_core_demo_test]": "true"})
        rest = listener.wait_for(
            lambda e: e["type"] == "charge.refunded"
            and e["data"]["object"]["payment_intent"] == intent["id"]
            and e["data"]["object"]["refunded"] is True,
            after=after_first,
        )
        (second,) = parse(listener, rest)

        assert (first.kind, first.amount_minor, first.payment_ref) == (EventKind.PAYMENT_PARTIALLY_REFUNDED, 300, intent["id"])
        assert (second.kind, second.amount_minor, second.payment_ref) == (EventKind.PAYMENT_REFUNDED, 1000, intent["id"])
