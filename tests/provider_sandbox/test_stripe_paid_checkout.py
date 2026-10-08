"""A Stripe Checkout this app CREATED, actually PAID in a browser, and the real events it leads to (specs/010 T029a). Manual, never CI.

tests/provider_sandbox/test_stripe_sandbox.py proves the adapter against real Stripe signatures and real refunds, but its purchase event came
from `stripe trigger`'s fixture (a guest checkout with no customer and no `metadata.product_ref`), because completing a HOSTED Checkout page needs
a browser. This drives one with Playwright (already a dev dependency for the e2e tier): the adapter creates the session, headless Chromium pays it
with Stripe's documented test card, Stripe sends the real `checkout.session.completed`, and the same payment is refunded through the API.

What this closes, that nothing else could show:
  * the session THIS APP made ties to its catalog entry: the completed event carries the Price id the adapter put in `metadata.product_ref`, and
    the customer is the one the adapter was told to bill (a hand-made session has neither);
  * `payment_ref` is the PaymentIntent, and the real refund of that payment names the very same one;
  * the amount on the event is what the buyer paid INCLUDING tax (a $5.00 Price is 550 under Managed Payments' 10% VAT): informational only, since the
    catalog decides credits, but it is not the Price.

What it does NOT do: it depends on the markup of Stripe's hosted page (a stable `data-testid` for the submit button and the `#cardNumber`-style
field ids), so a redesign can break it without the adapter being wrong. It skips when Playwright or its Chromium is not installed.
"""
import asyncio
import os
import re
import shutil
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.billing.providers.base import BillingCustomer, CreditProduct, EventKind
from app.billing.providers.stripe import StripeProvider
from tests.provider_sandbox.stripe_listener import StripeListener

pytestmark = pytest.mark.provider_sandbox

EVENTS = ("checkout.session.completed", "charge.refunded")
SUCCESS, CANCEL = "https://example.com/ok", "https://example.com/no"
TEST_CARD = ("4242424242424242", "12 / 34", "123")  # Stripe's documented test card: always succeeds


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


def pay_in_a_browser(url: str) -> None:
    """Pay a hosted Checkout page with the test card in headless Chromium. Skips (not fails) when no browser is available."""
    playwright_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (`playwright install chromium`)")
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except playwright_api.Error as exc:
            pytest.skip(f"Chromium is not installed for Playwright ({type(exc).__name__}): `playwright install chromium`")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1600})
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("#cardNumber", timeout=60_000)
            usd = page.get_by_text(re.compile(r"^\$\d+\.\d\d$"))
            if usd.count():  # Stripe localizes by IP and offers the Price's own currency beside it; keep the session's
                usd.first.click()
                page.wait_for_timeout(1_500)
            page.fill("#cardNumber", TEST_CARD[0])
            page.fill("#cardExpiry", TEST_CARD[1])
            page.fill("#cardCvc", TEST_CARD[2])
            page.fill("#billingName", "Agent Core Demo")
            if page.locator("#billingPostalCode").count():
                page.fill("#billingPostalCode", "94105")
            page.get_by_test_id("hosted-payment-submit-button").click()
            page.wait_for_url(re.compile(r"^https://example\.com/ok.*"), timeout=90_000)
        finally:
            browser.close()


@pytest.fixture(scope="module")
def paid(stripe_key, stripe_sandbox, listener):
    """The whole story once: this app's adapter makes the session, a browser pays it, Stripe delivers the completion, and the payment is refunded."""
    price = stripe_sandbox.pack(500)
    customer = stripe_sandbox.customer()
    stripe_sandbox.post(f"/v1/customers/{customer['id']}", {"email": "agent-core-demo@example.com"})  # a hosted page asks for one
    provider = StripeProvider(listener.secret, api_key=stripe_key)
    session = asyncio.run(
        provider.create_checkout(
            BillingCustomer("acme-tenant", "stripe", customer["id"]), CreditProduct("stripe", price["id"], Decimal(100)),
            idempotency_key=f"paid-{os.urandom(6).hex()}", success_url=SUCCESS, cancel_url=CANCEL,
        )
    )
    pay_in_a_browser(session.url)
    purchase = listener.wait_for(lambda e: e["type"] == "checkout.session.completed" and e["data"]["object"]["id"] == session.session_ref)
    (bought,) = provider.parse_webhook(purchase.headers, purchase.body)
    real = stripe_sandbox.get(f"/v1/checkout/sessions/{session.session_ref}")
    stripe_sandbox.post("/v1/refunds", {"payment_intent": real["payment_intent"], "metadata[agent_core_demo_test]": "true"})
    refund = listener.wait_for(lambda e: e["type"] == "charge.refunded" and e["data"]["object"]["payment_intent"] == real["payment_intent"])
    (reversed_,) = provider.parse_webhook(refund.headers, refund.body)
    return SimpleNamespace(price=price, customer=customer, session=session, real=real, bought=bought, reversed=reversed_)


class TestACheckoutThisAppCreatedAndAnActualBuyerPaid:
    def test_the_session_the_adapter_made_ends_up_complete_and_paid(self, paid):
        assert (paid.real["status"], paid.real["payment_status"]) == ("complete", "paid")
        assert paid.real["payment_intent"], "null until the session completes: this is where payment_ref comes from"

    def test_the_completed_event_is_a_purchase_for_the_customer_the_app_asked_to_bill(self, paid):
        assert paid.bought.kind is EventKind.CREDITS_PURCHASED
        assert paid.bought.customer_ref == paid.customer["id"]

    def test_it_names_the_catalog_entry_through_the_metadata_the_adapter_set(self, paid):
        """A session made any other way has no `metadata.product_ref`, and the app quarantines it: this one carries the Price id."""
        assert paid.bought.product_ref == paid.price["id"]

    def test_the_payment_ref_is_the_payment_intent(self, paid):
        assert paid.bought.payment_ref == paid.real["payment_intent"]

    def test_the_amount_on_the_event_is_what_the_buyer_paid_including_tax_not_the_price(self, paid):
        assert (paid.bought.amount_minor, paid.bought.currency) == (paid.real["amount_total"], paid.real["currency"])
        assert paid.bought.amount_minor >= 500, "never below the Price; above it where tax applies"

    def test_the_real_refund_of_that_payment_reverses_the_very_payment_that_was_bought(self, paid):
        assert paid.reversed.kind is EventKind.PAYMENT_REFUNDED
        assert paid.reversed.payment_ref == paid.bought.payment_ref, "the refund finds the grant it takes back"
