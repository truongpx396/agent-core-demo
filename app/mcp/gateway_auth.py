"""Who is calling the MCP gateway, proven from a bearer token (specs/017).

The gateway (`app/mcp/gateway.py`) is an OAuth 2.1 RESOURCE SERVER: it never issues a token, it checks the one
it is given. The MCP authorization spec asks that it validate signature and expiry, accept only a token issued
FOR IT (the `aud` claim, via the RFC 8707 `resource` the client sent), and never pass the token on. This
module is those checks plus one policy the SDK's contract has no place for.

The verifier never raises. `mcp.server.auth` calls `verify_token` inside its middleware, where an exception is
a 500 nobody decided. So every failure, unexpected ones included, is a `None` (the SDK then answers 401 with
the metadata URL a client uses to find the authorization server) and a counted, logged reason that is never
the token.

What a token must be:
- signed by a key in the authorization server's JWKS, with an algorithm from a short allow-list (RS256) taken
  from the allow-list and never from the token's own header (`none`, or an HS256 token "signed" with the
  public key, is refused before any key is used);
- `iss` equal to the configured issuer, `aud` containing THIS server's resource URI, `exp` and `sub` present,
  `exp` in the future (a few seconds of skew allowed). A token minted without a `resource` parameter has the
  client id as audience (measured on Casdoor 4.24, specs/017 research) and is refused, which is the point of
  the audience check;
- its `scope` claim (a space-separated string) is what the SDK enforces `required_scopes` against.

Signing keys: `JwksCache` fetches the JWKS with the event loop's own client, keeps it for `ttl_seconds`, and
refreshes on an unknown `kid` (how a rotated key is picked up), at most once per `min_refresh_seconds` so
made-up `kid`s can't make the gateway hammer the authorization server. Keys older than the TTL are not
trusted: if they can't be refreshed the token is refused with reason `jwks_unavailable` (a gap: to the client
that looks like an expired login, not an outage; the metric says which).
"""
import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence

import httpx
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core import metrics

logger = logging.getLogger(__name__)

JWKS_FETCH_TIMEOUT_SECONDS = 5.0


class JwksUnavailable(Exception):
    """The signing keys cannot be confirmed current: they could not be fetched, and the ones held are older than the TTL."""


class JwksCache:
    """The authorization server's signing keys, by `kid`."""

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: float,
        min_refresh_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=JWKS_FETCH_TIMEOUT_SECONDS)
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = asyncio.Lock()

    def _fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < self._ttl

    async def key_for(self, kid: str) -> jwt.PyJWK | None:
        """The key with this `kid`, or None if the authorization server has no such key. Raises `JwksUnavailable`."""
        now = self._clock()
        if self._fresh(now) and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = self._clock()
            if self._fresh(now) and kid in self._keys:  # another request refreshed while this one waited
                return self._keys[kid]
            if self._last_attempt is None or now - self._last_attempt >= self._min_refresh:
                await self._refresh(now)
            if not self._fresh(self._clock()):
                raise JwksUnavailable("the signing keys are older than the TTL and could not be refreshed")
            return self._keys.get(kid)

    async def aclose(self) -> None:
        """Close the HTTP client, if this cache made it (a test's injected one is the test's to close)."""
        if self._owns_client:
            await self._client.aclose()

    async def ready(self) -> bool:
        """True if the keys are current, trying a refresh (within the same rate limit) when they are not."""
        try:
            await self.key_for("")  # an unknown kid: it only asks whether a current key set can be had
        except JwksUnavailable:
            return False
        return True

    async def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            document = response.json()
            keys = {}
            for jwk in document["keys"]:
                try:
                    keys[jwk["kid"]] = jwt.PyJWK(jwk)
                except (jwt.PyJWTError, KeyError):  # a key of a type we do not use, or without a kid: skip it, keep the rest
                    logger.warning("mcp_gateway_jwk_skipped", extra={"kid": jwk.get("kid") if isinstance(jwk, dict) else None})
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("mcp_gateway_jwks_fetch_failed", extra={"error_class": type(exc).__name__})
            return
        self._keys = keys
        self._fetched_at = self._clock()


class JwtVerifier:
    """`mcp.server.auth.provider.TokenVerifier` for one gateway endpoint: valid for THIS server's resource URI only."""

    def __init__(
        self,
        *,
        server: str,
        issuer: str,
        audience: str,
        keys: JwksCache,
        algorithms: Sequence[str] = ("RS256",),
        leeway_seconds: int = 5,
    ) -> None:
        self._server = server
        self._issuer = issuer
        self._audience = audience
        self._keys = keys
        self._algorithms = tuple(algorithms)
        self._leeway = leeway_seconds

    def _reject(self, outcome: str, reason: str) -> AccessToken | None:
        metrics.agent_mcp_gateway_requests_total.labels(server=self._server, outcome=outcome).inc()
        # The reason is an exception class or a fixed phrase. Never the token, and never a claim of it.
        logger.info("mcp_gateway_token_rejected", extra={"server": self._server, "outcome": outcome, "reason": reason})
        return None

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            algorithm, kid = header.get("alg"), header.get("kid")
            if algorithm not in self._algorithms:
                return self._reject("invalid_token", "algorithm_not_allowed")
            if not isinstance(kid, str) or not kid:
                return self._reject("invalid_token", "no_kid")
            key = await self._keys.key_for(kid)
            if key is None:
                return self._reject("invalid_token", "unknown_kid")
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except JwksUnavailable:
            return self._reject("jwks_unavailable", "JwksUnavailable")
        except jwt.PyJWTError as exc:
            return self._reject("invalid_token", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - a verifier that raises turns a bad token into a 500; refuse it instead
            return self._reject("invalid_token", f"unexpected:{type(exc).__name__}")

        # `scope` is a space-separated string (RFC 6749), a list in some providers; anything else grants nothing rather than raising.
        scope = claims.get("scope", "")
        if isinstance(scope, str):
            scopes = scope.split()
        elif isinstance(scope, list):
            scopes = [s for s in scope if isinstance(s, str)]
        else:
            scopes = []
        metrics.agent_mcp_gateway_requests_total.labels(server=self._server, outcome="ok").inc()
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=str(claims["sub"]),
            claims=claims,
        )


class TenantGate:
    """ASGI middleware: of the callers with a VALID token, only these tenants may use this endpoint (403 for the rest).

    The operations server's data is the deployment's, not a tenant's, so any tenant's user holding a token with the right audience
    and scope could otherwise read it; an authorization server does not know which of its organizations are operators. Placed
    INSIDE the SDK's authentication middleware (it reads the verified token from the request's context) and outside the route, so
    a refused caller never reaches the MCP transport. A request with no token passes through: the route answers it with the 401 and
    the metadata URL a client needs to log in.
    """

    def __init__(self, app: ASGIApp, *, server: str, tenant_claim: str, allowed_tenants: frozenset[str]) -> None:
        self.app = app
        self._server = server
        self._tenant_claim = tenant_claim
        self._allowed = allowed_tenants

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        token = get_access_token() if scope["type"] == "http" else None
        if token is not None:
            tenant = (token.claims or {}).get(self._tenant_claim)
            if not isinstance(tenant, str) or tenant not in self._allowed:
                metrics.agent_mcp_gateway_requests_total.labels(server=self._server, outcome="forbidden_tenant").inc()
                logger.info("mcp_gateway_tenant_refused", extra={"server": self._server, "tenant": tenant if isinstance(tenant, str) else None})
                response = JSONResponse(
                    {"error": "forbidden", "error_description": "this tenant may not use this server"}, status_code=403
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
