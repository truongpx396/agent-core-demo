"""Who is calling the API, proven from the identity the front door signed (specs/018).

The front door (the authlimit service) verifies a person's access token, charges their buckets, and puts a SHORT-LIVED identity on the
request it forwards: a compact JWS, signed with a key only that service holds, naming a tenant (`tenant`) and a person (`sub`). In identity
mode the API trusts that and nothing else: not `X-Tenant-Id`, not `X-Principal-Id`, which a client can set to anything. This module is the
check. It does not issue anything, and it holds no secret: only the public half of the signing keys, fetched from the front door.

## What an identity must be

  * `alg` EdDSA, taken from this module's allow-list and never from the token's own header (an `alg: none`, or an RS256 external access
    token "presented" as an identity, is refused before any key is used); header `typ` `authlimit-identity+jwt`.
  * signed by a key the front door currently publishes (named by the header's `kid`).
  * `iss` `urn:agent-core:authlimit`, `aud` `agent-core-api`, `exp`, `nbf` and `sub` present, `exp` in the future and `nbf` not (a few seconds
    of clock skew allowed), and `tenant` and `sub` non-empty text. The identity's own lifetime is the front door's to set (30 seconds by
    default); the API does not widen it.
  * `testdata/auth-vectors/identity.json` is a real one, minted by the Go service, and a test here verifies it.

## The verifier never raises

`verify` is called from a request dependency. An exception there is a 500 and a path whose outcome nobody decided. So every failure,
including the unexpected ones, comes back as a `Refusal` carrying one counted, closed-vocabulary reason (`agent_api_identity_total{outcome}`)
and never the token or a claim of it. The reasons that are the CALLER'S fault (no header, a bad signature, expired) are `unavailable=False` and
become a 401; the one that is OURS, the public keys cannot be confirmed current, is `unavailable=True` and becomes a 503 (the request is not
the person's mistake and retrying shortly is the right thing to do).

## The public keys

`InternalKeys` fetches the key set with the event loop's own client under a short deadline, keeps it for `ttl_seconds`, and fetches again on
a `kid` it does not hold, which is how a rotated-in key is picked up, at most once per `min_refresh_seconds` however many identities with
made-up `kid`s arrive (a caller must not be able to turn that into load on the front door). If a fetch fails, the last keys stay trusted for
`stale_grace_seconds` more and the failure is counted (`agent_api_identity_key_refresh_total{result="error"}`); past that the API refuses
every identity with 503, because trusting a key set it cannot confirm is current is trusting a key that may have been withdrawn for compromise.
A grace exists because one dead authlimit replica behind a round-robin name would otherwise turn a single failed fetch into an outage for the
whole API. Fail CLOSED after the grace, never open: this is the one place the API decides who someone is.

## Starting up

`check_settings` is called once when the API starts. In identity mode with no usable `AUTHLIMIT_KEYS_URL` the API does not start, with a message
that names the variable (never its value, which could carry a credential): an API that cannot verify anyone must not come up.
"""
import asyncio
import ipaddress
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jwt

from app.core import metrics

logger = logging.getLogger(__name__)

IDENTITY_TYP = "authlimit-identity+jwt"
ISSUER = "urn:agent-core:authlimit"
AUDIENCE = "agent-core-api"
ALGORITHM = "EdDSA"
LEEWAY_SECONDS = 5
KEYS_FETCH_TIMEOUT_SECONDS = 5.0
# A real identity is about 400 characters. Anything near this is not one, and refusing it early bounds what a stranger can make this parse.
MAX_TOKEN_CHARS = 4096

# The closed vocabulary of `agent_api_identity_total{outcome}` (app/core/metrics.py), so the label cannot grow without a line changing here.
OUTCOMES = frozenset(
    {
        "ok", "missing", "malformed", "algorithm", "type", "unknown_kid", "signature", "expired", "not_yet_valid",
        "issuer", "audience", "claims", "unexpected", "keys_unavailable",
    }
)


class AuthConfigError(RuntimeError):
    """The API's authentication settings cannot work. Raised at startup; the message names the variable, never its value."""


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_settings(s) -> None:
    """Raise `AuthConfigError` if the API cannot verify identities as configured. Header mode needs nothing."""
    if s.auth_mode != "identity":
        return
    url = s.authlimit_keys_url.strip()
    parts = urlsplit(url)
    if not url or parts.scheme not in ("http", "https") or not parts.hostname:
        raise AuthConfigError("AUTHLIMIT_KEYS_URL must be set to an absolute http(s) URL when AUTH_MODE=identity: the API cannot verify anyone without the front door's keys")
    if parts.username or parts.password:
        raise AuthConfigError("AUTHLIMIT_KEYS_URL must not carry credentials: the front door's public keys are public")
    if parts.query or parts.fragment:
        raise AuthConfigError("AUTHLIMIT_KEYS_URL must not carry a query or fragment")
    # The keys are fetched by this process over an internal network, so an in-network name (a Docker service: no dot) may use plain http, as the
    # MCP gateway's key URL may. Anywhere else a person-in-the-middle could hand the API keys of their own, and then every identity.
    if parts.scheme == "http" and not (_is_loopback(parts.hostname) or "." not in parts.hostname):
        raise AuthConfigError("AUTHLIMIT_KEYS_URL must be https (plain http is accepted for a loopback address or an in-network name without a dot only)")


class KeysUnavailable(Exception):
    """The public keys cannot be confirmed current: they could not be fetched, and the ones held are older than the TTL plus the grace."""


class InternalKeys:
    """The front door's public signing keys, by `kid`."""

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float,
        stale_grace_seconds: float,
        min_refresh_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._grace = stale_grace_seconds
        self._min_refresh = min_refresh_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=KEYS_FETCH_TIMEOUT_SECONDS)
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    def _age(self, now: float) -> float | None:
        return None if self._fetched_at is None else now - self._fetched_at

    def _fresh(self, now: float) -> bool:
        age = self._age(now)
        return age is not None and age < self._ttl

    def _usable(self, now: float) -> bool:
        age = self._age(now)
        return age is not None and age < self._ttl + self._grace

    async def key_for(self, kid: str) -> jwt.PyJWK | None:
        """The key published under this `kid`, or None if the front door publishes no such key. Raises `KeysUnavailable`."""
        now = self._clock()
        if self._fresh(now) and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = self._clock()
            if self._fresh(now) and kid in self._keys:  # another request refreshed while this one waited
                return self._keys[kid]
            if self._last_attempt is None or now - self._last_attempt >= self._min_refresh:
                await self._refresh(now)
            if not self._usable(self._clock()):
                raise KeysUnavailable("the public keys are older than the TTL plus the grace and could not be refreshed")
            return self._keys.get(kid)

    async def aclose(self) -> None:
        """Close the HTTP client, if this object made it (a test's injected one is the test's to close)."""
        if self._owns_client:
            await self._client.aclose()

    async def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            keys = {}
            for jwk in response.json()["keys"]:
                try:
                    keys[jwk["kid"]] = jwt.PyJWK(jwk)
                except (jwt.PyJWTError, KeyError, TypeError):  # a key of a type we do not use, or without a kid: skip it, keep the rest
                    logger.warning("internal_identity_key_skipped")
            if not keys:
                raise ValueError("the key set has no usable key")
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
            metrics.agent_api_identity_key_refresh_total.labels(result="error").inc()
            logger.warning("internal_identity_keys_fetch_failed", extra={"error_class": type(exc).__name__})
            return
        self._keys = keys
        self._fetched_at = self._clock()
        metrics.agent_api_identity_key_refresh_total.labels(result="ok").inc()


@dataclass(frozen=True)
class Identity:
    """Who is calling, as the front door verified it: the only two facts the API builds a `SecurityCtx` from."""

    tenant: str
    principal: str


@dataclass(frozen=True)
class Refusal:
    """Why an identity was not accepted. `outcome` is the metric label; `unavailable` says the fault is ours (503), not the caller's (401)."""

    outcome: str
    unavailable: bool = False


# PyJWT's exceptions, most specific first, mapped to the closed vocabulary above.
_DECODE_OUTCOMES: tuple[tuple[type[Exception], str], ...] = (
    (jwt.ExpiredSignatureError, "expired"),
    (jwt.ImmatureSignatureError, "not_yet_valid"),
    (jwt.InvalidSignatureError, "signature"),
    (jwt.InvalidIssuerError, "issuer"),
    (jwt.InvalidAudienceError, "audience"),
    (jwt.MissingRequiredClaimError, "claims"),
    (jwt.exceptions.InvalidSubjectError, "claims"),  # PyJWT (2.10+, not exported at the top level) refuses a `sub` that is not text before our own check can see it
    (jwt.exceptions.InvalidIssuedAtError, "claims"),
    (jwt.exceptions.InvalidJTIError, "claims"),
    (jwt.InvalidAlgorithmError, "algorithm"),
)


class InternalIdentityVerifier:
    def __init__(self, keys: InternalKeys, *, leeway_seconds: int = LEEWAY_SECONDS) -> None:
        self._keys = keys
        self._leeway = leeway_seconds

    @staticmethod
    def _refuse(outcome: str, *, unavailable: bool = False) -> Refusal:
        metrics.agent_api_identity_total.labels(outcome=outcome).inc()
        # A fixed word, never the token and never a claim of it.
        logger.info("internal_identity_refused", extra={"outcome": outcome})
        return Refusal(outcome, unavailable)

    async def verify(self, token: str | None) -> Identity | Refusal:
        """The identity `token` proves, or the reason it does not. Never raises."""
        try:
            return await self._verify(token)
        except Exception as exc:  # noqa: BLE001 - a verifier that raises turns a bad header into a 500; refuse it instead
            logger.warning("internal_identity_unexpected_failure", extra={"error_class": type(exc).__name__})
            return self._refuse("unexpected")

    async def _verify(self, token: str | None) -> Identity | Refusal:
        if not token:
            return self._refuse("missing")
        if len(token) > MAX_TOKEN_CHARS:
            return self._refuse("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return self._refuse("malformed")
        if header.get("alg") != ALGORITHM:
            return self._refuse("algorithm")
        if header.get("typ") != IDENTITY_TYP:
            return self._refuse("type")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return self._refuse("unknown_kid")
        try:
            key = await self._keys.key_for(kid)
        except KeysUnavailable:
            return self._refuse("keys_unavailable", unavailable=True)
        if key is None:
            return self._refuse("unknown_kid")
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                leeway=self._leeway,
                options={"require": ["exp", "nbf", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            for error_type, outcome in _DECODE_OUTCOMES:
                if isinstance(exc, error_type):
                    return self._refuse(outcome)
            return self._refuse("malformed")
        tenant, principal = claims.get("tenant"), claims.get("sub")
        if not (isinstance(tenant, str) and tenant.strip() and isinstance(principal, str) and principal.strip()):
            return self._refuse("claims")
        metrics.agent_api_identity_total.labels(outcome="ok").inc()
        return Identity(tenant=tenant, principal=principal)
