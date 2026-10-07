"""The billing-provider port (specs/010-credit-billing-readiness/contracts/billing-provider-port.md).

A provider (Stripe, Polar, PayPal, or a metering product) is one adapter module that implements
`BillingProvider`. Everything else (the wallet, the inbox, the catalog, gating) is provider-agnostic and does
not change when an adapter is added (SC-004). An adapter holds NO business rules: it translates wire formats.

## What an adapter must never do

It must not name a tenant. `BillingEvent` has no `tenant` field on purpose: the only way a webhook reaches a
tenant is `billing_customers`, a link the app wrote itself, looked up by `customer_ref`. A payload that
claims a tenant ("tenant": "someone-else") therefore has nowhere to put it, which is a stronger guarantee than a
rule saying "ignore that field". It must not carry an amount that decides credits either: `amount_minor` is
informational, and what a purchase is worth is read from the server-side catalog (FR-015).

## Capabilities

Providers differ in shape: Stripe and Polar can take usage events, PayPal as far as found can only sell a
credit pack. `capabilities` says which optional methods exist, so one wallet serves all of them. An adapter
that declares no capability is a webhook-only provider, which is a complete, legal adapter.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Capability(Enum):
    CHECKOUT = "checkout"  # can start a purchase of a credit pack
    USAGE_EXPORT = "usage_export"  # accepts per-call usage events (Stripe meters, Polar events)
    BALANCE_READ = "balance_read"  # can report its own balance, for reconciliation only (never gating)


class EventKind(Enum):
    CREDITS_PURCHASED = "credits_purchased"  # a one-time pack was paid: grant the catalog's credits
    SUBSCRIPTION_PERIOD_STARTED = "subscription_period_started"  # a cycle began and includes credits
    PAYMENT_REFUNDED = "payment_refunded"  # the WHOLE payment was returned: claw its credits back (spec D6)
    # Part of a payment was returned. What that is worth in credits is a product decision nobody has made, so the
    # app does not guess: the event is quarantined and alerted for a person. An adapter must NEVER report a partial
    # refund as PAYMENT_REFUNDED (that would take back every credit) nor as IGNORED (that would keep them all).
    PAYMENT_PARTIALLY_REFUNDED = "payment_partially_refunded"
    DISPUTE_OPENED = "dispute_opened"  # a chargeback began
    DISPUTE_CLOSED = "dispute_closed"  # a chargeback ended
    IGNORED = "ignored"  # a type the app does not act on: recorded so it is not re-fetched


class InvalidSignature(Exception):
    """The delivery is not authentic (bad or missing signature, or outside the replay window). Carries no
    detail: what was wrong is information for an attacker, and the caller only needs to refuse."""


class InvalidPayload(ValueError):
    """The delivery IS authentic but its body is not in a shape this adapter understands."""


@dataclass(frozen=True)
class BillingEvent:
    """One provider event, normalized. Deliberately has no tenant (see the module docstring)."""

    provider: str
    event_id: str  # the PROVIDER's own event id: the inbox's dedupe key
    kind: EventKind
    customer_ref: str | None  # the provider's customer id; mapped to a tenant ONLY through billing_customers
    product_ref: str | None  # the provider's product/price id; mapped to credits ONLY through credit_products
    payment_ref: str | None  # the provider's payment/order id: ties a refund to the purchase it reverses
    amount_minor: int | None  # informational. Credits are NEVER computed from it
    currency: str | None
    occurred_at: datetime | None
    raw_type: str  # the provider's own type string, for the inbox row

    def stored(self) -> dict[str, Any]:
        """The ONLY thing the inbox keeps of a delivery: these normalized fields, never the raw body. A
        whitelist is the PII control: whatever else the provider put in the body (a buyer's name, email,
        address, card details) has no field here and so is never stored (constitution VI)."""
        return {
            "provider": self.provider,
            "event_id": self.event_id,
            "kind": self.kind.value,
            "customer_ref": self.customer_ref,
            "product_ref": self.product_ref,
            "payment_ref": self.payment_ref,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "raw_type": self.raw_type,
        }


@dataclass(frozen=True)
class BillingCustomer:
    tenant: str
    provider: str
    customer_ref: str


@dataclass(frozen=True)
class CreditProduct:
    provider: str
    product_ref: str
    credits: Decimal
    expires_after_days: int | None = None
    active: bool = True


@dataclass(frozen=True)
class CheckoutSession:
    session_ref: str
    url: str


@dataclass(frozen=True)
class UsageEvent:
    """What `export_usage` is given for one model call (the usage event's own figures)."""

    event_id: str  # the provider's idempotency key (Stripe `identifier`, Polar `external_id`)
    occurred_at: datetime
    credits: Decimal | None
    cost_usd: Decimal | None
    model: str | None = None
    total_tokens: int = 0


@dataclass(frozen=True)
class ExportFailure:
    event_id: str
    retryable: bool  # retryable: the outbox backs off and tries again; permanent: it gives up loudly


@dataclass(frozen=True)
class ExportResult:
    """A provider that reports a skipped duplicate (as Polar does) counts as success."""

    accepted: Sequence[str] = ()
    duplicate: Sequence[str] = ()
    failed: Sequence[ExportFailure] = field(default_factory=tuple)


@runtime_checkable
class BillingProvider(Protocol):
    name: str  # "stripe" | "polar" | "paypal" | "fake": the {provider} path segment
    capabilities: frozenset[Capability]

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[BillingEvent]:
        """Verify the signature FIRST (and any replay window the provider defines), then normalize.
        Raises `InvalidSignature` on any authenticity failure and `InvalidPayload` on an authentic but
        unreadable body. Pure: no I/O, no database. Never raises for an unknown event TYPE: that is an
        `IGNORED` event, recorded and left alone."""
        ...

    async def create_checkout(
        self, customer: BillingCustomer, product: CreditProduct, *, idempotency_key: str, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        """Only when CHECKOUT is declared."""
        ...

    async def export_usage(self, customer: BillingCustomer, events: Sequence[UsageEvent]) -> ExportResult:
        """Only when USAGE_EXPORT is declared. Uses `event.event_id` as the provider idempotency key."""
        ...

    async def read_balance(self, customer: BillingCustomer) -> Decimal:
        """Only when BALANCE_READ is declared. Used by reconciliation; never by gating."""
        ...
