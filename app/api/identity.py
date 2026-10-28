"""The API's side of "who is calling": the verifier it asks, the refusal it answers with, and the announcement of the mode it runs in (specs/018).

`app/core/internal_identity.py` is the check itself and knows nothing of HTTP. This is what the API adds around it:

  * one verifier per process, built the first time a request needs it from the settings (keys address, cache seconds, grace) and closed at
    shutdown. The HTTP client inside it belongs to the event loop that serves requests, so it is made there and not at import time.
  * `AuthRefused`, raised by `get_ctx` and turned by `auth_refused_handler` into the canonical envelope the front door itself answers with
    (`{code, message, details}`, the same words: tests/core/test_error_codes_authlimit.py), with the headers a client expects: a 401 carries
    `WWW-Authenticate: Bearer realm="agent-core-api"`, a 503 carries `Retry-After: 30`. The message is the registry's and never says why
    (a caller learns that it is not authenticated, not whether its identity was expired or forged: the counted outcome says that, to the operator).
  * the loud announcement of header mode: a startup warning and the gauge `agent_api_auth_mode{mode}`, kept alive by `keep_announcing_auth_mode`.
    The collector forgets a series five minutes after its last update (the same reason the usage reconciler re-sets its gauge every minute), so
    a gauge set once at startup would vanish and the alert on it (ApiHeaderAuthMode) could never see ten minutes of header mode.
"""
import asyncio
import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core import metrics
from app.core.config import settings
from app.core.errors import AUTH_ERRORS, ErrorCode, ErrorEnvelope
from app.core.internal_identity import (
    InternalIdentityVerifier,
    InternalKeys,
    Refusal,
    check_settings,
)

logger = logging.getLogger(__name__)

REALM = "agent-core-api"
RETRY_AFTER_SECONDS = 30
ANNOUNCE_INTERVAL_SECONDS = 60.0

_verifier: InternalIdentityVerifier | None = None
_keys: InternalKeys | None = None


def verifier() -> InternalIdentityVerifier:
    """This process's verifier, built on first use from the settings."""
    global _verifier, _keys
    if _verifier is None:
        check_settings(settings)  # a request can only get here in identity mode, and a missing address must not build a client that can never work
        _keys = InternalKeys(
            settings.authlimit_keys_url.strip(),
            ttl_seconds=settings.authlimit_keys_cache_seconds,
            stale_grace_seconds=settings.authlimit_keys_stale_grace_seconds,
        )
        _verifier = InternalIdentityVerifier(_keys)
    return _verifier


async def close() -> None:
    """Close the key client, if one was ever made. Called from the lifespan on the loop that used it."""
    global _verifier, _keys
    keys, _verifier, _keys = _keys, None, None
    if keys is not None:
        await keys.aclose()


class AuthRefused(Exception):
    """A request the API will not serve because it does not carry a valid identity (401), or because it cannot be checked right now (503)."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def refusal_for(refusal: Refusal) -> AuthRefused:
    """The caller's fault is a 401; ours (the keys cannot be confirmed current) is a 503 the caller may retry."""
    return AuthRefused(ErrorCode.AUTH_UNAVAILABLE if refusal.unavailable else ErrorCode.UNAUTHENTICATED)


async def auth_refused_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthRefused)  # registered for this class only; the handler type is Exception because Starlette's is
    status, message = AUTH_ERRORS[exc.code]
    headers = {"WWW-Authenticate": f'Bearer realm="{REALM}"'} if status == 401 else {"Retry-After": str(RETRY_AFTER_SECONDS)}
    return JSONResponse(ErrorEnvelope(exc.code, message, {}).to_dict(), status_code=status, headers=headers)


def announce_auth_mode(mode: str) -> None:
    """Set the gauge (1 for the mode in force, 0 for the other, so both series exist) and, in header mode, say so in the log."""
    for candidate in ("identity", "headers"):
        metrics.agent_api_auth_mode.labels(mode=candidate).set(1.0 if candidate == mode else 0.0)


def warn_if_header_mode(mode: str) -> None:
    """One line at startup. Metadata only: it names the mode and the consequence, never a header value."""
    if mode == "headers":
        logger.warning(
            "api_auth_mode_headers",
            extra={"auth_mode": mode, "consequence": "X-Tenant-Id and X-Principal-Id are trusted as sent; never run this where a stranger can reach it"},
        )
    else:
        logger.info("api_auth_mode", extra={"auth_mode": mode})


async def keep_announcing_auth_mode(mode: str, *, interval_seconds: float = ANNOUNCE_INTERVAL_SECONDS) -> None:
    """Re-set the gauge forever (until cancelled), so the series never ages out of the collector."""
    while True:
        announce_auth_mode(mode)
        await asyncio.sleep(interval_seconds)
