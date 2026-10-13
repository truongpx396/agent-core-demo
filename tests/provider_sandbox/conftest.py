"""Shared plumbing for the provider-sandbox tier (specs/010 T029): the operator's own test credentials, never printed.

Every fixture here self-skips when what it needs is missing, so `pytest -m provider_sandbox` on a machine with no sandbox is a clean
skip, not a failure. The one exception is deliberate: a LIVE-mode key makes the run FAIL before anything is created, because a skip
would let someone believe the check had run and a create would put test objects in a real account.
"""
import contextlib

import httpx
import pytest

from app.core import config

LABEL = {"metadata[agent_core_demo_test]": "true"}  # every object this tier creates carries it, so a sandbox can be tidied by search


class StripeSandbox:
    """The few Stripe REST calls the tests need, with the key held here and nowhere else. A failure names status, type, code and the
    offending field, never Stripe's message text (which can quote a key fragment)."""

    def __init__(self, key: str):
        self.key = key
        self._client = httpx.Client(base_url="https://api.stripe.com", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        self._cleanups: list = []

    def _check(self, response: httpx.Response, what: str) -> dict:
        if response.status_code != 200:
            error = response.json().get("error", {}) if response.headers.get("content-type", "").startswith("application/json") else {}
            raise AssertionError(
                f"Stripe {what}: HTTP {response.status_code} {error.get('type')} {error.get('code')} field={error.get('param')}"
            )
        return response.json()

    def post(self, path: str, form: dict | None = None) -> dict:
        return self._check(self._client.post(path, data=form or {}), f"POST {path}")

    def get(self, path: str, **params) -> dict:
        return self._check(self._client.get(path, params=params), f"GET {path}")

    def later(self, cleanup) -> None:
        self._cleanups.append(cleanup)

    def close(self) -> None:
        for cleanup in reversed(self._cleanups):
            with contextlib.suppress(Exception):  # tidying a sandbox must never turn a passing run red
                cleanup()
        self._client.close()

    # --- objects ----------------------------------------------------------------------------------------

    def customer(self) -> dict:
        customer = self.post("/v1/customers", {"name": "agent-core-demo sandbox test (safe to delete)", **LABEL})
        self.later(lambda: self._client.delete(f"/v1/customers/{customer['id']}"))
        return customer

    def pack(self, unit_amount: int = 500) -> dict:
        """A one-time Price on a fresh Product. The Product carries a `tax_code` because an account with Managed Payments enabled (the
        default) refuses a Checkout line item without one (HTTP 400, "the product tax code is missing")."""
        product = self.post("/v1/products", {"name": "agent-core-demo test pack (safe to delete)", "tax_code": "txcd_10103001", **LABEL})
        price = self.post("/v1/prices", {"product": product["id"], "unit_amount": str(unit_amount), "currency": "usd", **LABEL})
        self.later(lambda: self._client.post(f"/v1/prices/{price['id']}", data={"active": "false"}))
        self.later(lambda: self._client.post(f"/v1/products/{product['id']}", data={"active": "false"}))
        return price

    def paid_intent(self, amount: int = 1000) -> dict:
        """A succeeded payment made with Stripe's documented test card, so there is a charge to refund."""
        return self.post(
            "/v1/payment_intents",
            {
                "amount": str(amount), "currency": "usd", "payment_method": "pm_card_visa", "confirm": "true",
                "automatic_payment_methods[enabled]": "true", "automatic_payment_methods[allow_redirects]": "never", **LABEL,
            },
        )


def require_test_mode_key(key: str | None) -> str:
    """The key this tier may use. Unset skips (nothing ran); anything that is not a TEST-mode secret or restricted key FAILS, before a
    single request is made. Kept a plain function so a hermetic test can pin it: it is the one thing standing between this tier and a
    live account."""
    if not key:
        pytest.skip("STRIPE_API_KEY is not set (put a Stripe SANDBOX secret key in .env)")
    if not key.startswith(("sk_test_", "rk_test_")):
        pytest.fail("STRIPE_API_KEY is not a test-mode key; this tier creates objects and must never run against a live account")
    return key


@pytest.fixture(scope="session")
def stripe_key() -> str:
    return require_test_mode_key(config.STRIPE_API_KEY)


@pytest.fixture(scope="module")
def stripe_sandbox(stripe_key):
    sandbox = StripeSandbox(stripe_key)
    try:
        balance = sandbox.get("/v1/balance")
        assert balance["livemode"] is False, "a test-mode key must reach test mode"
    except (httpx.HTTPError, AssertionError) as exc:
        sandbox.close()
        pytest.skip(f"the Stripe sandbox is not reachable with this key ({type(exc).__name__})")
    yield sandbox
    sandbox.close()
