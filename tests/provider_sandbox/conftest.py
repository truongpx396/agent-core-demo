"""Shared plumbing for the provider-sandbox tier (specs/010 T029): the operator's own test credentials, never printed.

Every fixture here self-skips when what it needs is missing, so `pytest -m provider_sandbox` on a machine with no sandbox is a clean
skip, not a failure. The one exception is deliberate: a LIVE-mode key makes the run FAIL before anything is created, because a skip
would let someone believe the check had run and a create would put test objects in a real account.
"""
import contextlib
import os
import subprocess
import uuid

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


SHARE_LISTENER = "PROVIDER_SANDBOX_SHARE_LISTENER"


def other_listener(cli: str) -> str | None:
    """The pid of a `<cli> listen` that is ALREADY running, or None.

    `stripe trigger` and `polar trigger` (and every real refund this tier makes) are delivered to EVERY listening session of the account,
    so a developer's own `listen`, forwarding to their local app, would receive this tier's sample events too and their app would act on
    them (a purchase for a customer it has never heard of is quarantined in their database). The tier does not do that to anyone."""
    found = subprocess.run(["pgrep", "-f", f"{cli} listen"], capture_output=True, text=True).stdout.split()
    others = [pid for pid in found if pid != str(os.getpid())]
    return others[0] if others else None


def refuse_beside_a_foreign_listener(cli: str) -> None:
    pid = other_listener(cli)
    if pid and os.environ.get(SHARE_LISTENER) != "1":
        pytest.skip(
            f"a `{cli} listen` is already running (pid {pid}); this tier's events would reach its session and whatever it forwards to. "
            f"Stop it and run again, or set {SHARE_LISTENER}=1 to accept that"
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


# --- Polar -----------------------------------------------------------------------------------------------------------------------------

POLAR_SANDBOX_API = "https://sandbox-api.polar.sh/v1"  # fixed here, never read from a setting: this tier cannot be pointed at production


def require_polar_sandbox(token: str | None, environment: str) -> str:
    """The token this tier may use. Unset skips (nothing ran); a deployment configured for PRODUCTION FAILS before a single request is made,
    because Polar's tokens look the same in both environments and this tier creates and deletes objects. Kept a plain function so a hermetic
    test can pin it."""
    if not token:
        pytest.skip("POLAR_ACCESS_TOKEN is not set (put an Organization Access Token from sandbox.polar.sh in .env)")
    if environment != "sandbox":
        pytest.fail("POLAR_ENVIRONMENT is not 'sandbox'; this tier creates objects and must never run against a production organization")
    return token


class PolarSandbox:
    """The few Polar REST calls the tests need, with the token held here and nowhere else. A failure names status, error type and the
    offending field, never Polar's text (which can quote what was sent)."""

    def __init__(self, token: str):
        self._client = httpx.Client(base_url=POLAR_SANDBOX_API, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        self._cleanups: list = []

    @staticmethod
    def _check(response: httpx.Response, what: str) -> dict:
        if response.status_code >= 300:
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            detail = body.get("detail")
            where = detail[0].get("loc") if isinstance(detail, list) and detail and isinstance(detail[0], dict) else None
            raise AssertionError(f"Polar {what}: HTTP {response.status_code} {body.get('error')} field={where}")
        return response.json() if response.content else {}

    def post(self, path: str, body: dict) -> dict:
        return self._check(self._client.post(path, json=body), f"POST {path}")

    def get(self, path: str, **params) -> dict:
        return self._check(self._client.get(path, params=params), f"GET {path}")

    def later(self, cleanup) -> None:
        self._cleanups.append(cleanup)

    def close(self) -> None:
        for cleanup in reversed(self._cleanups):
            with contextlib.suppress(Exception):  # tidying a sandbox must never turn a passing run red
                cleanup()
        self._client.close()

    def customer(self) -> dict:
        customer = self.post(
            "/customers/",
            {"email": f"agent-core-demo+{uuid.uuid4().hex[:10]}@mailinator.com", "name": "agent-core-demo sandbox test (safe to delete)", "metadata": LABEL_JSON},
        )
        self.later(lambda: self._client.delete(f"/customers/{customer['id']}"))
        return customer

    def pack(self, price_amount: int = 500) -> dict:
        """A one-time product with a fixed USD price, archived afterwards (Polar does not delete products)."""
        product = self.post(
            "/products/",
            {
                "name": "agent-core-demo test pack (safe to delete)", "recurring_interval": None, "metadata": LABEL_JSON,
                "prices": [{"amount_type": "fixed", "price_amount": price_amount, "price_currency": "usd"}],
            },
        )
        self.later(lambda: self._client.patch(f"/products/{product['id']}", json={"is_archived": True}))
        return product


LABEL_JSON = {"agent_core_demo_test": "true"}  # every object this tier creates carries it, so a sandbox can be tidied by search


@pytest.fixture(scope="session")
def polar_token() -> str:
    return require_polar_sandbox(config.POLAR_ACCESS_TOKEN, config.POLAR_ENVIRONMENT)


@pytest.fixture(scope="module")
def polar_sandbox(polar_token):
    sandbox = PolarSandbox(polar_token)
    try:
        sandbox.get("/organizations/")
    except (httpx.HTTPError, AssertionError) as exc:
        sandbox.close()
        pytest.skip(f"the Polar sandbox is not reachable with this token ({type(exc).__name__})")
    yield sandbox
    sandbox.close()
