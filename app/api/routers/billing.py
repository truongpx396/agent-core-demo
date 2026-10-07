"""`POST /billing/webhooks/{provider}`: money in (specs/010-credit-billing-readiness, T021).

This route is deliberately NOT behind the tenant-identity headers (`deps.get_ctx`): a payment provider cannot
send them, so the caller is unauthenticated and its authenticity is the provider's SIGNATURE and nothing else.
It is built for that, in the order the contract fixes (contracts/billing-provider-port.md):

  1. A provider that is not configured is a 404, counted. (A configured one with no secret never gets this far:
     settings refuse to load, so the process does not start. See `validate_configuration`.)
  2. The body is size-capped BEFORE it is read: an unauthenticated endpoint must not buffer what it is sent.
  3. The adapter verifies the signature before anything is stored or decided; a bad one is a 400, counted, and
     NOTHING is stored (not even an inbox row: a forger does not get to fill the table).
  4-7. `webhooks.process_event`: dedupe in the inbox, resolve the tenant through the link the app wrote, apply in
     one transaction, and answer 2xx only after the commit. Anything that fails before that is a 5xx, so the
     provider retries.

The response body says nothing about WHY (no tenant, no balance, no reason): the caller is a stranger until the
signature verifies, and even then it is a machine that only needs the status code.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.billing import providers, webhooks
from app.billing.providers.base import BillingProvider, InvalidPayload, InvalidSignature
from app.core import metrics
from app.core.config import (
    BILLING_PROVIDERS,
    BILLING_WEBHOOK_MAX_BODY_BYTES,
    BILLING_WEBHOOK_SECRETS,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# The adapters this process serves, built once and on demand (tests re-point it). `validate_configuration`
# builds it at startup so a misconfigured deployment fails to start instead of failing its first payment.
_providers: dict[str, BillingProvider] | None = None


def configured_providers() -> dict[str, BillingProvider]:
    global _providers
    if _providers is None:
        _providers = providers.build_configured(BILLING_PROVIDERS, BILLING_WEBHOOK_SECRETS)
    return _providers


def validate_configuration() -> None:
    """Called from the app's lifespan. Raises (so the process refuses to start) for an enabled provider that
    no adapter is registered under or that has no signing secret."""
    configured_providers()


def _count(provider: str, outcome: str) -> None:
    metrics.agent_billing_webhook_total.labels(provider=provider, outcome=outcome).inc()


class _TooLarge(Exception):
    pass


async def _read_capped(request: Request, cap: int) -> bytes:
    """The body, or `_TooLarge` as soon as it is known to exceed `cap`. A declared Content-Length over the cap is
    refused without reading a byte; an undeclared or understated one is cut off while streaming."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise _TooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/billing/webhooks/{provider}", include_in_schema=False)
async def receive_webhook(provider: str, request: Request) -> JSONResponse:
    adapter = configured_providers().get(provider)
    if adapter is None:
        _count("unknown", "unknown_provider")  # never the caller's own string as a label value
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    try:
        body = await _read_capped(request, BILLING_WEBHOOK_MAX_BODY_BYTES)
    except _TooLarge:
        _count(provider, "too_large")
        return JSONResponse({"detail": "Payload Too Large"}, status_code=413)

    try:
        events = adapter.parse_webhook(dict(request.headers), body)
    except InvalidSignature:
        _count(provider, "invalid_signature")
        logger.warning("billing_webhook_invalid_signature", extra={"provider": provider})
        return JSONResponse({"detail": "Bad Request"}, status_code=400)
    except InvalidPayload:
        _count(provider, "invalid_payload")
        return JSONResponse({"detail": "Bad Request"}, status_code=400)

    outcomes = [await webhooks.process_event(event) for event in events]
    # A retryable result wins over a final one: the provider must redeliver the whole delivery, and the events
    # that did apply are duplicates on the second pass, which is exactly what the inbox is for.
    if "failed" in outcomes:
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    if "retry" in outcomes:
        return JSONResponse({"detail": "Service Unavailable"}, status_code=503)
    return JSONResponse({"received": len(events)}, status_code=200)
