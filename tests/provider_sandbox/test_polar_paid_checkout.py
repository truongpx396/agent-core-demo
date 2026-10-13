"""A Polar Checkout this app CREATED, actually PAID in a browser, and the real events it leads to (specs/010 T029b). Manual, never CI.

tests/provider_sandbox/test_polar_sandbox.py proves the adapter against real Polar signatures and a real checkout API, but its purchase and refund
events are `polar trigger` SAMPLES (a generator's payloads, signed by Polar's real delivery path), because paying a hosted Polar checkout takes a
browser. This drives one with Playwright (already a dev dependency for the e2e tier), the Polar counterpart of test_stripe_paid_checkout.py: the
adapter creates the session, headless Chromium pays it with Stripe's documented test card, Polar delivers the real `order.paid`, and the order
is refunded through Polar's API.

What this closes, that nothing else could show:
  * the order a buyer ACTUALLY paid is a `CREDITS_PURCHASED` for the customer the adapter was told to bill, for the product the adapter put in
    the session, and its `payment_ref` is the order id the API reports (a sample's ids are random, so none of this was ever checked);
  * the order's `checkout_id` is the session THIS APP made, and the whole real lifecycle (every checkout, customer, order and refund delivery
    the purchase and its refund produced) grants exactly once and reverses exactly once: `order.created` and `order.updated` of the very same purchase carry
    `billing_reason: purchase` too and must stay ignored;
  * the real full refund names the same order, and a second `create_checkout` for the same attempt AFTER it was paid returns the paid
    session instead of a second checkout (the adapter's "already paid is returned" rule, never run against a paid session before);
  * the buyer's typed name, billing address and email never reach the stored form of the event (a sample's buyer is a random fake).

Facts only a real payment showed: Polar's hosted page needs a billing address for a US buyer (street, postal code, city, state) and the card
number, expiry and CVC live in ONE Stripe Elements iframe; after paying, Polar redirects to the success URL with a `customer_session_token`
appended (a bearer credential for the buyer's portal, so this file never prints the URL).

What it does NOT do: it depends on the markup of Polar's hosted page (the Payment Element iframe's title, its `number`/`expiry`/`cvc` inputs,
the `billing-country` and `billing-state` test ids, the `customer_billing_address.*` field names and the "Pay now" button), so a redesign can
break it without the adapter being wrong. It skips when Playwright or its Chromium is not installed. Each run leaves one refunded order in
the sandbox (an order cannot be deleted); the product is archived and the customer deletion is attempted.

Run it sparingly, never in a loop: Polar rate-limits the sandbox (100 requests a minute is documented; the hosted page makes calls of its own,
not measured here), and a token that ran sixteen of these in about 80 minutes was locked out for an hour (`Retry-After: 3570`). The helper then
says so (`HTTP 429, the sandbox rate limit; retry after N s`) and the run must not be repeated until then.
"""
import asyncio
import os
import re
import shutil
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.billing.providers.base import BillingCustomer, CreditProduct, EventKind
from app.billing.providers.polar import PolarProvider
from tests.provider_sandbox.polar_listener import PolarListener

pytestmark = pytest.mark.provider_sandbox

SUCCESS, CANCEL = "https://example.com/ok", "https://example.com/no"
TEST_CARD = ("4242424242424242", "12 / 34", "123")  # Stripe's documented test card: always succeeds
BUYER = {"name": "Agent Core Demo Buyer", "street": "1 Market Street", "postal_code": "94105", "city": "San Francisco"}


@pytest.fixture(scope="module")
def listener(polar_token):
    if shutil.which("polar") is None:
        pytest.skip("the `polar` CLI is not installed (curl -fsSL https://polar.sh/install.sh | bash, then `polar auth login`)")
    live = PolarListener()
    try:
        live.start()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    yield live
    live.stop()


def pay_in_a_browser(url: str) -> None:
    """Pay a hosted Polar checkout with the test card in headless Chromium. Skips (not fails) when no browser is available."""
    playwright_api = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed (`playwright install chromium`)")
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except playwright_api.Error as exc:
            pytest.skip(f"Chromium is not installed for Playwright ({type(exc).__name__}): `playwright install chromium`")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1600})
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            card = page.frame_locator("iframe[title='Secure payment input frame']").first
            card.locator("input[name='number']").wait_for(timeout=60_000)
            card.locator("input[name='number']").fill(TEST_CARD[0])
            card.locator("input[name='expiry']").fill(TEST_CARD[1])
            card.locator("input[name='cvc']").fill(TEST_CARD[2])
            page.fill("input[name='customer_name']", BUYER["name"])
            page.get_by_test_id("billing-country").click()
            page.get_by_role("option", name="United States").click()
            page.fill("input[name='customer_billing_address.line1']", BUYER["street"])  # these four appear only once a country is chosen
            page.fill("input[name='customer_billing_address.postal_code']", BUYER["postal_code"])
            page.fill("input[name='customer_billing_address.city']", BUYER["city"])
            page.get_by_test_id("billing-state").click()
            page.get_by_role("option", name="California").click()
            page.get_by_role("button", name="Pay now").click()
            page.wait_for_url(re.compile(r"^https://example\.com/ok.*"), timeout=90_000)
        finally:
            browser.close()


@pytest.fixture(scope="module")
def paid(polar_token, polar_sandbox, listener):
    """The whole story once: this app's adapter makes the session, a browser pays it, Polar delivers the order, and the order is refunded."""
    product, customer = polar_sandbox.pack(500), polar_sandbox.customer()
    provider = PolarProvider(listener.secret, access_token=polar_token, environment="sandbox")
    billing, catalog, key = BillingCustomer("acme-tenant", "polar", customer["id"]), CreditProduct("polar", product["id"], Decimal(100)), f"paid-{os.urandom(6).hex()}"
    start = len(listener.deliveries())  # what the listener's own readiness samples sent is before this point

    def checkout():
        return asyncio.run(provider.create_checkout(billing, catalog, idempotency_key=key, success_url=SUCCESS, cancel_url=CANCEL))

    session = checkout()
    pay_in_a_browser(session.url)
    purchase = listener.wait_for(lambda e: e.get("type") == "order.paid" and e["data"].get("customer_id") == customer["id"], after=start)
    (bought,) = provider.parse_webhook(purchase.headers, purchase.body)
    order = polar_sandbox.get(f"/orders/{purchase.event['data']['id']}")
    real_checkout = polar_sandbox.get(f"/checkouts/{session.session_ref}")
    again = checkout()  # the same attempt, asked for after it was paid
    polar_sandbox.post("/refunds/", {"order_id": order["id"], "reason": "customer_request", "amount": order["total_amount"], "revoke_benefits": False,
                                     "comment": "agent-core-demo test"})
    refund = listener.wait_for(lambda e: e.get("type") == "order.refunded" and e["data"].get("id") == order["id"], after=start)
    (reversed_,) = provider.parse_webhook(refund.headers, refund.body)
    lifecycle = [event for delivery in listener.deliveries()[start:] for event in provider.parse_webhook(delivery.headers, delivery.body)]
    return SimpleNamespace(
        product=product, customer=customer, session=session, again=again, order=order, checkout=real_checkout, bought=bought,
        reversed=reversed_, lifecycle=lifecycle,
    )


class TestACheckoutThisAppCreatedAndAnActualBuyerPaid:
    def test_the_session_the_adapter_made_ends_up_succeeded_and_its_order_is_paid(self, paid):
        assert paid.checkout["status"] == "succeeded"
        assert (paid.order["status"], paid.order["billing_reason"]) == ("paid", "purchase")

    def test_the_order_belongs_to_the_session_this_app_created(self, paid):
        assert paid.order["checkout_id"] == paid.session.session_ref

    def test_the_paid_event_is_a_purchase_for_the_customer_the_app_asked_to_bill(self, paid):
        assert paid.bought.kind is EventKind.CREDITS_PURCHASED
        assert paid.bought.customer_ref == paid.customer["id"]

    def test_it_names_the_catalog_entry_through_the_product_the_adapter_put_in_the_session(self, paid):
        assert paid.bought.product_ref == paid.product["id"]

    def test_the_payment_ref_is_the_order_id_the_api_reports(self, paid):
        assert paid.bought.payment_ref == paid.order["id"]

    def test_the_amount_on_the_event_is_the_orders_total_not_the_product_price(self, paid):
        assert (paid.bought.amount_minor, paid.bought.currency) == (paid.order["total_amount"], paid.order["currency"])
        assert paid.bought.amount_minor >= 500, "never below the price; above it where tax applies"

    def test_the_real_full_refund_reverses_the_very_order_that_was_bought(self, paid):
        assert paid.reversed.kind is EventKind.PAYMENT_REFUNDED
        assert paid.reversed.payment_ref == paid.bought.payment_ref, "the refund finds the grant it takes back"
        assert paid.reversed.amount_minor == paid.order["total_amount"]

    def test_the_whole_real_lifecycle_grants_exactly_once_and_reverses_exactly_once(self, paid):
        """`order.created` and `order.updated` of this purchase say `billing_reason: purchase` as well; only `order.paid` may grant."""
        kinds = [event.kind for event in paid.lifecycle]

        assert kinds.count(EventKind.CREDITS_PURCHASED) == 1
        assert kinds.count(EventKind.PAYMENT_REFUNDED) == 1
        assert {event.raw_type for event in paid.lifecycle} >= {"order.created", "order.updated", "order.paid", "order.refunded"}

    def test_asking_again_for_a_paid_attempt_returns_the_paid_session_not_a_second_checkout(self, paid):
        assert paid.again.session_ref == paid.session.session_ref

    def test_the_buyers_typed_details_never_reach_the_stored_form(self, paid):
        stored = str(paid.bought.stored()).lower()

        for detail in (*BUYER.values(), paid.customer["email"]):
            assert detail.lower() not in stored
