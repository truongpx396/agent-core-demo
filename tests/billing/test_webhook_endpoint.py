"""POST /billing/webhooks/{provider}: the defensive shell around the inbox (app/api/routers/billing.py).

The caller is an unauthenticated stranger until a signature verifies, so what is proven here is what the route
does BEFORE it trusts anything: refuse an unknown provider, refuse an oversized body before reading it, refuse a
forgery without storing a thing, and answer with a status the provider's retry logic can act on. The inbox, the
tenant link and the wallet behind it (`webhooks.process_event`) are faked here and proven against a real
Postgres in tests/integration/test_billing_webhooks_real_postgres.py.

A real `TestClient` over a tiny app that mounts only this router: the route is read as a Starlette request
(streamed body, raw headers), which calling the handler as a plain function would not exercise.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import billing as billing_router
from app.billing import providers, webhooks
from app.billing.providers.fake import FakeProvider
from app.core import metrics
from tests.billing.contract import NOW
from tests.conftest import metric_value

SECRET = "whsec_endpoint"


def _count(provider: str, outcome: str) -> float:
    return metric_value(metrics.agent_billing_webhook_total, provider=provider, outcome=outcome)


@pytest.fixture
def fake_provider():
    return FakeProvider(SECRET, clock=lambda: NOW)


@pytest.fixture
def received(monkeypatch, fake_provider):
    """The events `process_event` was handed, and the outcome it will report."""
    state = {"events": [], "outcome": "applied"}

    async def process_event(event):
        state["events"].append(event)
        return state["outcome"]

    monkeypatch.setattr(billing_router, "_providers", {"fake": fake_provider})
    monkeypatch.setattr(webhooks, "process_event", process_event)
    return state


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(billing_router.router)
    return TestClient(app)


def signed(fake_provider, **overrides):
    payload = {"id": "evt_1", "type": "purchase.completed", "customer": "cus_1", "product": "pack_100", "payment": "pay_1", "created": NOW}
    payload.update(overrides)
    return fake_provider.sign(payload)


class TestAValidDelivery:
    def test_is_applied_and_acknowledged_without_any_tenant_identity(self, client, received, fake_provider):
        """A payment provider cannot send X-Tenant-Id, so this route must not need it (every chat route 422s without)."""
        headers, body = signed(fake_provider)

        response = client.post("/billing/webhooks/fake", content=body, headers=headers)

        assert response.status_code == 200 and response.json() == {"received": 1}
        (event,) = received["events"]
        assert (event.event_id, event.customer_ref) == ("evt_1", "cus_1")

    def test_a_tenant_header_the_sender_adds_changes_nothing(self, client, received, fake_provider):
        headers, body = signed(fake_provider)

        client.post("/billing/webhooks/fake", content=body, headers={**headers, "X-Tenant-Id": "someone-else"})

        (event,) = received["events"]
        assert not hasattr(event, "tenant") and "someone-else" not in str(event.stored())

    @pytest.mark.parametrize("outcome", ["applied", "duplicate", "ignored", "quarantined"])
    def test_a_finished_event_is_acknowledged_so_the_provider_stops_retrying(self, client, received, fake_provider, outcome):
        """Including `quarantined`: retrying an unlinked customer cannot fix it, and the alert is what tells a person."""
        received["outcome"] = outcome
        headers, body = signed(fake_provider)

        assert client.post("/billing/webhooks/fake", content=body, headers=headers).status_code == 200

    def test_a_refund_waiting_for_its_purchase_is_answered_503_so_the_provider_retries(self, client, received, fake_provider):
        received["outcome"] = "retry"
        headers, body = signed(fake_provider)

        assert client.post("/billing/webhooks/fake", content=body, headers=headers).status_code == 503

    def test_a_failure_to_apply_is_answered_500_so_the_provider_retries(self, client, received, fake_provider):
        received["outcome"] = "failed"
        headers, body = signed(fake_provider)

        assert client.post("/billing/webhooks/fake", content=body, headers=headers).status_code == 500


class TestAnUnknownProvider:
    def test_is_a_404_and_counted_under_a_fixed_label(self, client, received, fake_provider):
        headers, body = signed(fake_provider)
        before = _count("unknown", "unknown_provider")

        response = client.post("/billing/webhooks/paypal", content=body, headers=headers)

        assert response.status_code == 404
        assert _count("unknown", "unknown_provider") == before + 1
        assert received["events"] == []

    def test_never_mints_a_metric_label_from_the_callers_own_string(self, client, received, fake_provider):
        """An unauthenticated route that labelled a counter with the path segment would let a stranger create
        unbounded label values."""
        headers, body = signed(fake_provider)

        client.post("/billing/webhooks/zzz-attacker-chosen-name", content=body, headers=headers)

        assert _count("zzz-attacker-chosen-name", "unknown_provider") == 0


class TestTheBodyCap:
    @pytest.fixture(autouse=True)
    def small_cap(self, monkeypatch):
        monkeypatch.setattr(billing_router, "BILLING_WEBHOOK_MAX_BODY_BYTES", 2048)

    @pytest.fixture
    def parsed(self, monkeypatch, fake_provider):
        """Records whether the adapter was ever asked to look at a body."""
        seen = []
        real = fake_provider.parse_webhook

        def spy(headers, body):
            seen.append(len(body))
            return real(headers, body)

        monkeypatch.setattr(fake_provider, "parse_webhook", spy)
        return seen

    def test_a_declared_length_over_the_cap_is_refused_before_the_adapter_ever_sees_a_body(self, client, received, parsed):
        before = _count("fake", "too_large")

        response = client.post("/billing/webhooks/fake", content=b"x" * 5000)

        assert response.status_code == 413
        assert parsed == [] and received["events"] == []
        assert _count("fake", "too_large") == before + 1

    def test_a_body_streamed_with_no_declared_length_is_cut_off_at_the_cap(self, client, received, parsed):
        """A sender can lie about, or omit, Content-Length: the cap must hold while reading, not only in the header."""
        def chunks():
            for _ in range(10):
                yield b"x" * 1024

        response = client.post("/billing/webhooks/fake", content=chunks())

        assert response.status_code == 413
        assert parsed == []

    def test_a_body_exactly_at_the_cap_is_read(self, client, received, fake_provider, parsed):
        headers, body = signed(fake_provider)
        padded = body + b" " * (2048 - len(body))  # trailing whitespace is valid JSON but changes the MAC: sign the padded form
        headers, padded = fake_provider.sign({"id": "evt_1", "type": "purchase.completed", "pad": "p" * 1700, "created": NOW})
        assert len(padded) <= 2048

        response = client.post("/billing/webhooks/fake", content=padded, headers=headers)

        assert response.status_code == 200 and parsed == [len(padded)]


class TestAForgery:
    def test_a_bad_signature_is_a_400_counted_and_never_reaches_the_inbox(self, client, received, fake_provider):
        headers, body = signed(fake_provider)
        before = _count("fake", "invalid_signature")

        response = client.post("/billing/webhooks/fake", content=body + b" ", headers=headers)

        assert response.status_code == 400
        assert _count("fake", "invalid_signature") == before + 1
        assert received["events"] == [], "nothing is decided, stored or granted for a forgery"

    def test_a_missing_signature_is_a_400(self, client, received, fake_provider):
        _, body = signed(fake_provider)

        assert client.post("/billing/webhooks/fake", content=body).status_code == 400

    def test_the_refusal_does_not_say_why(self, client, received, fake_provider):
        headers, body = signed(fake_provider)

        response = client.post("/billing/webhooks/fake", content=body + b" ", headers=headers)

        assert response.json() == {"detail": "Bad Request"}

    def test_an_authentic_body_in_an_unknown_shape_is_a_400_counted_separately(self, client, received, fake_provider):
        headers, body = fake_provider.sign({"not": "an event"})
        before = _count("fake", "invalid_payload")

        response = client.post("/billing/webhooks/fake", content=body, headers=headers)

        assert response.status_code == 400 and _count("fake", "invalid_payload") == before + 1


class TestStartup:
    def test_an_enabled_provider_with_no_registered_adapter_stops_the_process(self, monkeypatch):
        monkeypatch.setattr(billing_router, "_providers", None)
        monkeypatch.setattr(billing_router, "BILLING_PROVIDERS", ("stripe",))
        monkeypatch.setattr(billing_router, "BILLING_WEBHOOK_SECRETS", {"stripe": "whsec"})

        with pytest.raises(providers.UnknownProvider, match="no billing adapter"):
            billing_router.validate_configuration()

    def test_an_enabled_provider_with_no_secret_stops_the_process(self, monkeypatch):
        monkeypatch.setattr(billing_router, "_providers", None)
        monkeypatch.setattr(billing_router, "BILLING_PROVIDERS", ("fake",))
        monkeypatch.setattr(billing_router, "BILLING_WEBHOOK_SECRETS", {})

        with pytest.raises(providers.UnknownProvider, match="no secret"):
            billing_router.validate_configuration()

    def test_a_provider_with_a_blank_secret_stops_the_process(self, monkeypatch):
        monkeypatch.setattr(billing_router, "_providers", None)
        monkeypatch.setattr(billing_router, "BILLING_PROVIDERS", ("fake",))
        monkeypatch.setattr(billing_router, "BILLING_WEBHOOK_SECRETS", {"fake": ""})

        with pytest.raises(providers.UnknownProvider):
            billing_router.validate_configuration()

    def test_a_deployment_that_enables_nothing_serves_no_provider(self, monkeypatch):
        monkeypatch.setattr(billing_router, "_providers", None)
        monkeypatch.setattr(billing_router, "BILLING_PROVIDERS", ())
        monkeypatch.setattr(billing_router, "BILLING_WEBHOOK_SECRETS", {})

        billing_router.validate_configuration()

        assert billing_router.configured_providers() == {}
