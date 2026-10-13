"""An in-repo provider that signs and parses its own payloads: the port's reference implementation.

It exists so the whole money-in path (signature, inbox, link, catalog, wallet) is exercised end to end
without a sandbox account, and so `tests/billing/contract.py` has an adapter to hold the contract against
before any real one exists (D5). It is not a payment provider: nobody is charged anything. A deployment
should enable it (`BILLING_PROVIDERS=fake`) only for development and tests.

## Wire format (invented here; a real adapter follows its provider's documented one)

    X-Fake-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>
    body: {"id", "type", "customer", "product", "payment", "amount_minor", "currency", "created", ...}

The shape is borrowed from the common signed-webhook pattern (a timestamp bound into the MAC so a captured
delivery cannot be replayed outside the tolerance window). The body also carries fields the app must NEVER
trust or store ("tenant", "credits", buyer details) precisely so the contract tests can prove it ignores them.
"""
import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.billing.providers.base import (
    BillingCustomer,
    BillingEvent,
    Capability,
    EventKind,
    ExportFailure,
    ExportResult,
    InvalidPayload,
    InvalidSignature,
    UsageEvent,
)

SIGNATURE_HEADER = "x-fake-signature"
TOLERANCE_SECONDS = 300

_KINDS = {
    "purchase.completed": EventKind.CREDITS_PURCHASED,
    "subscription.period_started": EventKind.SUBSCRIPTION_PERIOD_STARTED,
    "payment.refunded": EventKind.PAYMENT_REFUNDED,
    "payment.partially_refunded": EventKind.PAYMENT_PARTIALLY_REFUNDED,
    "dispute.opened": EventKind.DISPUTE_OPENED,
    "dispute.closed": EventKind.DISPUTE_CLOSED,
}


class FakeProvider:
    name = "fake"
    # Declared on the CLASS so the usage-event write can ask "does any enabled provider bill on usage?" without a secret or
    # an instance (providers.usage_export_providers). The fake accepts usage so the whole outbox path is provable end to end.
    capabilities: frozenset[Capability] = frozenset({Capability.USAGE_EXPORT})

    def __init__(self, secret: str, *, clock: Callable[[], float] = time.time):
        if not secret:
            raise ValueError("a provider needs a webhook secret")
        self._secret = secret.encode()
        self._clock = clock
        # What this "provider" has been told, by event id: its own idempotency ledger, in memory. A real one is remote.
        self.received: dict[str, tuple[str, UsageEvent]] = {}
        self.calls: list[list[str]] = []  # the event ids of every export_usage call, in order
        self.spans: list[tuple[float, float]] = []  # (started, finished) of each call on the monotonic clock: lets a test prove two overlapped
        self.delay = 0.0  # seconds each call takes: lets a test hold a batch in flight while another worker looks
        self._script: list[str] = []

    def script(self, *steps: str) -> None:
        """Scripts the next export_usage calls, one step each: "ok", "retryable" (every event reported as a retryable
        failure), "permanent" (reported as a permanent one) or "raise" (the call itself fails, as a network error does).
        Once the script runs out, calls succeed."""
        self._script.extend(steps)

    # --- signing: how a test (or a developer) plays the provider ---------------------------------------

    def sign(self, payload: Mapping[str, Any], *, at: float | None = None) -> tuple[dict[str, str], bytes]:
        """(headers, body) for a delivery of `payload`, signed now (or at `at`)."""
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        stamp = int(self._clock() if at is None else at)
        return {SIGNATURE_HEADER: f"t={stamp},v1={self._mac(stamp, body)}"}, body

    def _mac(self, stamp: int, body: bytes) -> str:
        return hmac.new(self._secret, f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()

    # --- the port --------------------------------------------------------------------------------------

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[BillingEvent]:
        self._verify({k.lower(): v for k, v in headers.items()}, body)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise InvalidPayload("body is not JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise InvalidPayload("body has no event id")
        raw_type = str(data.get("type") or "")
        return [
            BillingEvent(
                provider=self.name,
                event_id=data["id"],
                kind=_KINDS.get(raw_type, EventKind.IGNORED),
                customer_ref=_text(data.get("customer")),
                product_ref=_text(data.get("product")),
                payment_ref=_text(data.get("payment")),
                amount_minor=data["amount_minor"] if isinstance(data.get("amount_minor"), int) else None,
                currency=_text(data.get("currency")),
                occurred_at=_when(data.get("created")),
                raw_type=raw_type[:100],
            )
        ]

    def _verify(self, headers: Mapping[str, str], body: bytes) -> None:
        header = headers.get(SIGNATURE_HEADER)
        if not header:
            raise InvalidSignature
        parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
        try:
            stamp = int(parts["t"])
            claimed = parts["v1"]
        except (KeyError, ValueError):
            raise InvalidSignature from None
        # Constant-time compare, and the MAC is checked BEFORE the window, so a wrong signature and a stale
        # one are indistinguishable from outside.
        if not hmac.compare_digest(claimed, self._mac(stamp, body)) or abs(self._clock() - stamp) > TOLERANCE_SECONDS:
            raise InvalidSignature

    async def create_checkout(self, *args, **kwargs):
        raise NotImplementedError("the fake provider declares no CHECKOUT capability")

    async def export_usage(self, customer: BillingCustomer, events: Sequence[UsageEvent]) -> ExportResult:
        """Idempotent by event id, like Stripe's `identifier` and Polar's `external_id`: sending an id it has already
        recorded is reported as a duplicate (a success), never recorded twice."""
        self.calls.append([event.event_id for event in events])
        started = time.monotonic()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            step = self._script.pop(0) if self._script else "ok"
            if step == "raise":
                raise ConnectionError("the provider is unreachable")
            if step in ("retryable", "permanent"):
                return ExportResult(failed=[ExportFailure(event.event_id, retryable=step == "retryable") for event in events])
            accepted, duplicate = [], []
            for event in events:
                if event.event_id in self.received:
                    duplicate.append(event.event_id)
                else:
                    self.received[event.event_id] = (customer.customer_ref, event)
                    accepted.append(event.event_id)
            return ExportResult(accepted=accepted, duplicate=duplicate)
        finally:
            self.spans.append((started, time.monotonic()))

    async def read_balance(self, *args, **kwargs):
        raise NotImplementedError("the fake provider declares no BALANCE_READ capability")


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC) if value is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None
