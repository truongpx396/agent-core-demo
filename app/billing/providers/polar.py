"""The Polar adapter: sells a prepaid credit pack and tells the app it was paid (specs/010 T029b, D16).

Same shape and the same promise as the Stripe adapter: Polar takes the money and nothing more, so this declares `CHECKOUT` only (no
`USAGE_EXPORT`, no `BALANCE_READ`), translates two wire formats and holds no business rule: a Checkout going out, a signed webhook
coming in.

## What was checked against Polar, and how

Every claim below is from Polar's documentation, from Polar's own Python SDK (1.0.2, read as the ORACLE for the wire formats), or from a
run against Polar's SANDBOX (2026-10-08), not from memory.

  * **The signature** is the Standard Webhooks scheme: headers `webhook-id`, `webhook-timestamp` (seconds) and `webhook-signature` (a
    SPACE-separated list of `v1,<base64>`, several while a secret is rolled); the MAC is HMAC-SHA256 over `"<id>.<floor(timestamp)>.<body>"`.
    What is unusual is the KEY. Polar's docs say a secret created before 2026-09-08 is used as the UTF-8 bytes of the whole string, and one
    created on or after that date is a Standard Webhooks `whsec_` secret whose base64 remainder is decoded. A secret does not say which it
    is, so (exactly as Polar's SDK does) BOTH derived keys are tried. The secret `polar listen --print-secret` printed in the sandbox was 32
    characters with no `whsec_` prefix and verified under the raw-bytes key. Unlike Stripe's verifier, Polar's refuses a timestamp in
    the FUTURE as well as a stale one (300 s either way); that is mirrored here.
  * **The event id is the `webhook-id` header.** The body has none (`{type, timestamp, api_version, data}`), and Polar's SDK documentation
    names that header "a durable deduplication key". It is covered by the MAC, so it cannot be altered in transit.
  * **The events** (docs and the SDK's models, identical across API versions 2026-04, 2026-10 and 2027-01): `order.paid` ("the order is
    fully processed and payment has been received") whose `data` is an Order with `billing_reason` (`purchase` for a one-time product),
    `status`, `customer_id`, `product_id`, `total_amount`, `currency`; and `order.refunded` ("fully or partially refunded") whose Order
    `status` is `refunded` or `partially_refunded` and whose `refunded_amount` is the running total. That `status` is exactly the
    distinction the app needs between `PAYMENT_REFUNDED` and `PAYMENT_PARTIALLY_REFUNDED`.
  * **There is no dispute event.** Neither the docs, the SDK nor `polar trigger --list` has one; a Refund carries a `dispute` object only
    for a dispute Polar PREVENTED by refunding. So `DISPUTE_OPENED`/`DISPUTE_CLOSED` are never produced by this adapter (disclosed).
  * **Checkout has no idempotency.** The docs mention no key, Polar's SDK guidance says to reconcile at the application level before
    retrying a mutation, and a real sandbox run proved a sent `Idempotency-Key` header is ignored (the same body and key made two
    sessions). `create_checkout` therefore looks for an existing session of this attempt first; see its docstring.

## How a purchase is tied to a refund and to a catalog entry

`BillingEvent.payment_ref` is the ORDER id: it is on `order.paid` and on every later `order.refunded` for that order, so a refund finds the
grant it takes back. The catalog entry (`credit_products.product_ref`) is the Polar PRODUCT id, which an Order carries directly (unlike
Stripe, nothing has to travel through metadata). An order with no single product (`product_id` null) has no catalog entry and the app
quarantines it as `unknown_product`.

## What this adapter must never do

The same list as the Stripe adapter's: name a tenant, let a payload decide an amount, keep buyer details (a Polar Order carries the
buyer's name, billing address and email; only a whitelist is ever read), put a tenant on the wire, or repeat a provider's own words in an
error (they are reported by status, error type and the NAME of the field Polar objected to).

## Not done here, and said so

  * Subscriptions are ignored: an `order.paid` whose `billing_reason` is anything but `purchase` grants nothing.
  * Nothing calls `create_checkout` yet: no route starts a purchase.
  * Two concurrent `create_checkout` calls with the SAME key can each create a session (the list-then-create is not atomic and Polar offers
    no lock). Only an unpaid session results; two PAYMENTS would be two real orders, each correctly granted once.
"""
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from app.billing.providers.base import (
    BillingCustomer,
    BillingEvent,
    Capability,
    CheckoutError,
    CheckoutSession,
    CreditProduct,
    EventKind,
    ExportResult,
    InvalidPayload,
    InvalidSignature,
    UsageEvent,
)

TOLERANCE_SECONDS = 300  # Polar's SDK default, applied in BOTH directions
API_BASES = {"sandbox": "https://sandbox-api.polar.sh/v1", "production": "https://api.polar.sh/v1"}
REQUEST_TIMEOUT_SECONDS = 30.0
ATTEMPT_KEY = "agent_core_attempt"  # the checkout metadata key that carries this attempt's idempotency key
LIST_LIMIT = 100  # Polar's maximum page: how many of a customer's newest sessions for a product are searched for an attempt
MAX_EVENT_ID_CHARS = 255

# Polar names the request field it objected to by a path ("body", "products", 0): safe to repeat once it is only identifier characters.
_FIELD = re.compile(r"[A-Za-z0-9_.\[\]]{1,100}")
_ERROR_TYPE = re.compile(r"[A-Za-z0-9_]{1,64}")


class PolarProvider:
    name = "polar"
    # On the CLASS, so the usage-event write can ask which providers bill on usage without a secret or an instance. Prepaid packs (D16).
    capabilities: frozenset[Capability] = frozenset({Capability.CHECKOUT})

    def __init__(
        self,
        secret: str,
        *,
        access_token: str | None = None,
        environment: str | None = None,
        clock: Callable[[], float] = time.time,
        tolerance_seconds: int = TOLERANCE_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str | None = None,
    ):
        if not secret:
            raise ValueError("a provider needs a webhook secret")
        if tolerance_seconds <= 0:
            raise ValueError("a tolerance of 0 disables the replay check")
        self._keys = _signing_keys(secret)
        self._clock = clock
        self._tolerance = tolerance_seconds
        self._transport = transport
        if access_token is None or environment is None:
            from app.core.config import (  # read at construction, not import: tests re-point the settings
                POLAR_ACCESS_TOKEN,
                POLAR_ENVIRONMENT,
            )

            access_token = POLAR_ACCESS_TOKEN if access_token is None else access_token
            environment = POLAR_ENVIRONMENT if environment is None else environment
        if environment not in API_BASES:
            raise ValueError(f"environment must be one of {sorted(API_BASES)}, not {environment!r}")
        self._api_base = api_base or API_BASES[environment]
        self._token = access_token or ""

    # --- the webhook, inbound ---------------------------------------------------------------------------

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[BillingEvent]:
        lowered = {k.lower(): v for k, v in headers.items()}
        self._verify(lowered, body)
        event_id = lowered["webhook-id"]
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise InvalidPayload("body is not JSON") from exc
        obj = data.get("data") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not _text(data.get("type")) or not isinstance(obj, dict) or len(event_id) > MAX_EVENT_ID_CHARS:
            raise InvalidPayload("body is not a Polar event")
        raw_type = data["type"]
        kind, customer, product, payment, amount, currency = _normalize(raw_type, obj)
        return [
            BillingEvent(
                provider=self.name,
                event_id=event_id,
                kind=kind,
                customer_ref=customer,
                product_ref=product,
                payment_ref=payment,
                amount_minor=amount,
                currency=currency,
                occurred_at=_when(data.get("timestamp")),
                raw_type=raw_type[:100],
            )
        ]

    def _verify(self, headers: Mapping[str, str], body: bytes) -> None:
        webhook_id, stamp_text, signatures = headers.get("webhook-id"), headers.get("webhook-timestamp"), headers.get("webhook-signature")
        if not webhook_id or not stamp_text or not signatures:
            raise InvalidSignature
        try:
            stamp = float(stamp_text)
        except ValueError:
            raise InvalidSignature from None
        if not math.isfinite(stamp):
            raise InvalidSignature
        candidates: list[bytes] = []
        for part in signatures.split():
            version, sep, value = part.partition(",")
            if version != "v1" or not sep:  # any other version is ignored: no downgrade to a scheme this code does not know
                continue
            try:
                candidates.append(base64.b64decode(value, validate=True))
            except (binascii.Error, ValueError):
                continue
        if not candidates:
            raise InvalidSignature
        content = f"{webhook_id}.{math.floor(stamp)}.".encode() + body
        # Constant-time against EVERY candidate under EVERY derived key, accumulated without short-circuiting, and the MAC is checked
        # BEFORE the window, so a wrong signature and a stale one are indistinguishable from outside.
        matched = False
        for key in self._keys:
            expected = hmac.new(key, content, hashlib.sha256).digest()
            for candidate in candidates:
                matched |= hmac.compare_digest(expected, candidate)
        now = self._clock()
        if not matched or stamp < now - self._tolerance or stamp > now + self._tolerance:
            raise InvalidSignature

    # --- checkout, outbound -----------------------------------------------------------------------------

    async def create_checkout(
        self, customer: BillingCustomer, product: CreditProduct, *, idempotency_key: str, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        """A hosted Checkout for the pack's Polar product, for Polar customer `customer.customer_ref`.

        Polar has NO idempotency for this call (a sent `Idempotency-Key` is ignored; verified against the sandbox), so a retry of the same
        purchase attempt is made safe here by RECONCILING first: the attempt's key is stored in the session's metadata, and before creating
        one the customer's newest sessions for this product are listed and searched for it.

          * found and OPEN or already paid: that session is returned (never a second purchase for one attempt);
          * found but EXPIRED or FAILED: it cannot be paid, so a fresh session is created under the same key (the newest match wins next time);
          * not found: one is created. The search covers the newest `LIST_LIMIT` sessions of that customer and product.

        The tenant is deliberately not sent: Polar knows its own customer id, and the link from that id to a tenant is this app's table."""
        if not self._token:
            raise CheckoutError("POLAR_ACCESS_TOKEN is not set", retryable=False, code="no_access_token")
        if not idempotency_key or len(idempotency_key) > 255:
            raise CheckoutError("an idempotency key is 1 to 255 characters", retryable=False, code="bad_idempotency_key")
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(base_url=self._api_base, timeout=REQUEST_TIMEOUT_SECONDS, transport=self._transport) as client:
            existing = await self._find_attempt(client, headers, customer, product, idempotency_key)
            if existing is not None:
                return existing
            body = {
                "products": [product.product_ref],
                "customer_id": customer.customer_ref,
                "success_url": success_url,
                "return_url": cancel_url,
                "metadata": {ATTEMPT_KEY: idempotency_key},
            }
            response = await _send(client.post("/checkouts/", json=body, headers=headers))
        if response.status_code not in (200, 201):
            raise _api_error(response)
        session = _session(_json(response))
        if session is None:
            raise CheckoutError("Polar answered with no session", retryable=False, status=response.status_code, code="bad_response")
        return session

    async def _find_attempt(
        self, client: httpx.AsyncClient, headers: dict[str, str], customer: BillingCustomer, product: CreditProduct, key: str
    ) -> CheckoutSession | None:
        params = {"customer_id": customer.customer_ref, "product_id": product.product_ref, "limit": str(LIST_LIMIT), "sorting": "-created_at"}
        response = await _send(client.get("/checkouts/", params=params, headers=headers))
        if response.status_code != 200:
            raise _api_error(response)  # never create blind after a failed look: that is how a retry buys twice
        items = _json(response).get("items")
        if not isinstance(items, list):
            # A look that cannot be read is a look that failed: creating now is the blind create this search exists to prevent.
            raise CheckoutError("Polar answered the session search with no list", retryable=False, status=200, code="bad_response")
        for item in items:
            metadata = item.get("metadata") if isinstance(item, dict) else None
            if not isinstance(metadata, dict) or metadata.get(ATTEMPT_KEY) != key:
                continue
            if item.get("status") in ("expired", "failed"):
                continue  # dead: nothing can be paid on it, so a new session cannot double a purchase
            session = _session(item)
            if session is None:
                raise CheckoutError("Polar listed this attempt with no usable session", retryable=False, status=200, code="bad_response")
            return session
        return None

    # --- capabilities this adapter does not declare (D16) ------------------------------------------------

    async def export_usage(self, customer: BillingCustomer, events: Sequence[UsageEvent]) -> ExportResult:
        raise NotImplementedError("the polar adapter declares no USAGE_EXPORT capability (spec D16: prepaid packs)")

    async def read_balance(self, *args, **kwargs):
        raise NotImplementedError("the polar adapter declares no BALANCE_READ capability")


def _signing_keys(secret: str) -> tuple[bytes, ...]:
    """Every key a secret can mean: its own UTF-8 bytes (Polar's HMAC before 2026-09-08) and its base64 remainder decoded (Standard
    Webhooks). Mirrors `polar.webhooks._signing_keys` in Polar's SDK."""
    keys = [secret.encode()]
    remainder = secret.removeprefix("whsec_")
    try:
        decoded = base64.b64decode(remainder + "=" * (-len(remainder) % 4), validate=True)
    except (binascii.Error, ValueError):
        return tuple(keys)
    if decoded and decoded != keys[0]:
        keys.append(decoded)
    return tuple(keys)


def _normalize(raw_type: str, obj: Mapping[str, Any]) -> tuple[EventKind, str | None, str | None, str | None, int | None, str | None]:
    """(kind, customer, product, payment, amount_minor, currency) for one event. Only the fields the app needs are read: an Order carries
    the buyer's name, billing address and email, and none of that may be reached for."""
    ignored = (EventKind.IGNORED, None, None, None, None, None)
    reason = obj.get("billing_reason")
    if raw_type == "order.paid":
        # One-time packs only (D16), and only once PAID. Anything that is not positively a `purchase` grants nothing.
        if reason != "purchase" or obj.get("status") != "paid":
            return ignored
        return EventKind.CREDITS_PURCHASED, _customer(obj), _text(obj.get("product_id")), _text(obj.get("id")), _int(obj.get("total_amount")), _text(obj.get("currency"))
    if raw_type == "order.refunded":
        if isinstance(reason, str) and reason.startswith("subscription"):
            return ignored  # a renewal's refund reverses no pack this app granted
        # `status` is `refunded` only for a FULL refund (the event also fires for a partial one); the safe reading of anything else is
        # the one that quarantines for a person, not the one that claws every credit back (D12).
        kind = EventKind.PAYMENT_REFUNDED if obj.get("status") == "refunded" else EventKind.PAYMENT_PARTIALLY_REFUNDED
        return kind, _customer(obj), None, _text(obj.get("id")), _int(obj.get("refunded_amount")), _text(obj.get("currency"))
    return ignored


def _customer(obj: Mapping[str, Any]) -> str | None:
    nested = obj.get("customer")
    return _text(obj.get("customer_id")) or (_text(nested.get("id")) if isinstance(nested, dict) else None)


def _session(item: Any) -> CheckoutSession | None:
    if not isinstance(item, dict):
        return None
    ref, url = item.get("id"), item.get("url")
    return CheckoutSession(session_ref=ref, url=url) if isinstance(ref, str) and isinstance(url, str) and ref and url else None


async def _send(call) -> httpx.Response:
    try:
        return await call
    except httpx.HTTPError as exc:
        raise CheckoutError(f"could not reach Polar ({type(exc).__name__})", retryable=True, code="network") from exc


def _json(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _api_error(response: httpx.Response) -> CheckoutError:
    """By status, error type and the NAME of the offending field only: Polar's `detail` text can quote what was sent."""
    body = _json(response)
    kind = body.get("error") if isinstance(body.get("error"), str) and _ERROR_TYPE.fullmatch(body["error"]) else None
    field = None
    detail = body.get("detail")
    if isinstance(detail, list) and detail and isinstance(detail[0], dict) and isinstance(detail[0].get("loc"), list):
        parts = [str(p) for p in detail[0]["loc"] if p != "body"]
        candidate = ".".join(parts)
        field = candidate if _FIELD.fullmatch(candidate) else None
    status = response.status_code
    # 408/409 a request in flight, 429 a rate limit, 5xx Polar's own trouble: worth trying again. Any other 4xx is the request being
    # wrong (or the token lacking a scope) and trying again will say the same.
    retryable = status in (408, 409, 429) or status >= 500
    text = f"Polar refused the checkout (HTTP {status}" + (f", {kind}" if kind else "") + (f", field {field}" if field else "") + ")"
    return CheckoutError(text, retryable=retryable, status=status, code=kind)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
