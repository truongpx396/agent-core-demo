"""The MCP gateway: the two published servers over Streamable HTTP, behind OAuth 2.1 (specs/017).

`app/mcp/server.py` and `ops_server.py` speak stdio: a child process of whoever launched it, one client each, no port, and an
identity taken from that person's environment. That is right for a desktop assistant on the same machine and wrong for anything
else (a remote agent, a team, several replicas): there is no launcher to trust. This module serves the SAME tools over HTTP
with the SAME rule, "the identity is never an argument", but takes it from the verified access token of each request.

## Shape

  * One `MCPServer` per endpoint, each its own OAuth RESOURCE: `/ecorp/mcp` (scope `ecorp:read`) and `/ops/mcp` (scope `ops:read`).
    The audience a token must carry is that endpoint's own URL (RFC 8707), so a token for one is useless at the other, and the
    scope is checked too: the authorization server does not stop a client asking for a token whose audience and scope disagree
    (measured on Casdoor), so neither check alone is the boundary.
  * Stateless (`stateless_http`, `json_response`): no session to pin a client to a replica, so any replica serves any request and
    `docker compose up --scale mcp-gateway=N` behind the Caddyfile's `dynamic a` load-balances them. A tool call reads the token of
    its own request, and concurrent callers of different tenants never see each other's (tested).
  * The SDK does the protocol parts: the bearer middleware, the 401 with `WWW-Authenticate: Bearer resource_metadata=...`, the 403
    `insufficient_scope`, the RFC 9728 metadata document, the Host/Origin (DNS-rebinding) guard. What is ours is
    `gateway_auth.JwtVerifier` (signature, issuer, audience, expiry), `TenantGate` (which tenants may use the operations server)
    and `identity.token_source` (tenant and principal from the claims).
  * The metadata documents must sit at the HOST root (`/.well-known/oauth-protected-resource/ecorp/mcp`), not under a mounted path,
    where a client looks for them; so the two apps are not mounted but dispatched by exact path.

## Fail closed

Without an issuer and a JWKS URL the gateway does not start (`GatewayConfigError`); without `MCP_GATEWAY_OPS_TENANTS` the
operations endpoint refuses every caller; a token that cannot be verified, for any reason, is a 401. An `http://` issuer or public
URL is accepted only for a loopback host. Nothing here ever reads `MCP_TENANT`/`MCP_PRINCIPAL`.

## Not built (specs/017)

Per-tenant rate limits, a mutating tool (the sandbox is deliberately not behind this gateway: constitution Principle II puts every
side effect behind an approval gate that lives in the agent's graph, and an outside caller does not pass through it), stale-key
grace when the authorization server is down, and production TLS/DNS wiring (the `gateway` compose profile is for local use).

Run with: `make mcp-gateway-up` (the whole stack), or `python -m app.mcp.gateway --host 0.0.0.0 --port 8000`.
"""
import argparse
import contextlib
import ipaddress
import logging
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings
from app.core.config import settings as app_settings
from app.core.logging_config import configure_logging
from app.core.telemetry import configure_telemetry
from app.mcp import ops_server
from app.mcp import server as ecorp_server
from app.mcp.gateway_auth import JwksCache, JwtVerifier, TenantGate
from app.mcp.identity import token_source

logger = logging.getLogger(__name__)

GATEWAY_REFUSAL = "Refused: this request's access token has no usable tenant and principal."

ECORP_SCOPE = "ecorp:read"
OPS_SCOPE = "ops:read"


class GatewayConfigError(Exception):
    """The gateway is not configured well enough to serve anyone safely."""


def _is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(name: str, value: str, *, allow_plain_http_for: Callable[[str], bool], allow_path: bool) -> str:
    """`value` without a trailing slash, or `GatewayConfigError`. Plain http only where `allow_plain_http_for(host)` says so."""
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if not value or parts.scheme not in ("http", "https") or not parts.hostname:
        raise GatewayConfigError(f"{name} must be set to an absolute http(s) URL (got {value!r})")
    if parts.query or parts.fragment or (parts.path and not allow_path):
        raise GatewayConfigError(f"{name} must not carry a path, query or fragment (got {value!r})")
    if parts.scheme == "http" and not allow_plain_http_for(parts.hostname):
        raise GatewayConfigError(f"{name} must be https (plain http is accepted for a loopback host only; got {value!r})")
    return value


@dataclass(frozen=True)
class GatewayConfig:
    public_url: str
    issuer: str
    jwks_url: str
    tenant_claim: str
    principal_claim: str
    ops_tenants: frozenset[str]
    jwks_ttl_seconds: int

    @classmethod
    def from_settings(cls, s: Settings) -> "GatewayConfig":
        public_url = _check_url("MCP_GATEWAY_PUBLIC_URL", s.mcp_gateway_public_url, allow_plain_http_for=_is_loopback, allow_path=False)
        issuer = _check_url("MCP_GATEWAY_ISSUER", s.mcp_gateway_issuer, allow_plain_http_for=_is_loopback, allow_path=True)
        # The keys are fetched by this service, not shown to a client, so an in-network name (a Docker service: no dot) may use plain http.
        jwks_url = s.mcp_gateway_jwks_url.strip()
        _check_url("MCP_GATEWAY_JWKS_URL", jwks_url, allow_plain_http_for=lambda host: _is_loopback(host) or "." not in host, allow_path=True)
        tenant_claim, principal_claim = s.mcp_gateway_tenant_claim.strip(), s.mcp_gateway_principal_claim.strip()
        if not tenant_claim or not principal_claim:
            raise GatewayConfigError("MCP_GATEWAY_TENANT_CLAIM and MCP_GATEWAY_PRINCIPAL_CLAIM must name the claims that carry them")
        return cls(
            public_url=public_url,
            issuer=issuer,
            jwks_url=jwks_url,
            tenant_claim=tenant_claim,
            principal_claim=principal_claim,
            ops_tenants=frozenset(t.strip() for t in s.mcp_gateway_ops_tenants.split(",") if t.strip()),
            jwks_ttl_seconds=s.mcp_gateway_jwks_cache_seconds,
        )


@dataclass(frozen=True)
class _Endpoint:
    name: str
    path: str
    scope: str
    build: Callable[..., MCPServer]
    gated_tenants: frozenset[str] | None  # None: any tenant with a valid token; else only these


class _Dispatch:
    """Send a request to the app that owns its exact path. Not a Mount: see the module docstring."""

    def __init__(self, table: dict[str, ASGIApp]) -> None:
        self._table = table

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = self._table.get(scope["path"]) if scope["type"] == "http" else None
        if app is None:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
            return
        await app(scope, receive, send)


def _transport_security(public_url: str) -> TransportSecuritySettings:
    """Only the public Host and Origin are answered. A browser tab on another site that resolves its name to this gateway (DNS
    rebinding) sends a different Host, and is refused with a 421 before any tool runs."""
    parts = urlsplit(public_url)
    default_port = 443 if parts.scheme == "https" else 80
    host_ports = {parts.hostname or "", f"{parts.hostname}:{parts.port or default_port}"}
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(host_ports),
        allowed_origins=[public_url],
    )


def create_app(config: GatewayConfig | None = None, *, http_client: httpx.AsyncClient | None = None) -> Starlette:
    """The ASGI app. `http_client` is for tests (a stand-in authorization server's key endpoint)."""
    cfg = config or GatewayConfig.from_settings(app_settings)
    keys = JwksCache(cfg.jwks_url, ttl_seconds=cfg.jwks_ttl_seconds, client=http_client)
    security = _transport_security(cfg.public_url)
    identity = token_source(tenant_claim=cfg.tenant_claim, principal_claim=cfg.principal_claim)

    endpoints = [
        _Endpoint("ecorp", "/ecorp/mcp", ECORP_SCOPE, ecorp_server.build_server, gated_tenants=None),
        _Endpoint("ops", "/ops/mcp", OPS_SCOPE, ops_server.build_server, gated_tenants=cfg.ops_tenants),
    ]
    if not cfg.ops_tenants:
        logger.warning("mcp_gateway_ops_closed", extra={"reason": "MCP_GATEWAY_OPS_TENANTS is empty: the operations server refuses every caller"})

    table: dict[str, ASGIApp] = {}
    children: list[Starlette] = []
    for ep in endpoints:
        resource = f"{cfg.public_url}{ep.path}"
        server_kwargs: dict[str, Any] = {
            "token_verifier": JwtVerifier(server=ep.name, issuer=cfg.issuer, audience=resource, keys=keys),
            # Validated from a dict so the strings stay exactly as configured. Wrapping them in `AnyHttpUrl(...)` first normalises a
            # URL with no path to end in "/", and the metadata document would then name an authorization server whose identifier
            # differs, by that slash, from the `issuer` in its own metadata and in every token (RFC 8414 compares them exactly).
            "auth": AuthSettings.model_validate(
                {
                    "issuer_url": cfg.issuer,
                    "resource_server_url": resource,
                    "required_scopes": [ep.scope],
                    "validate_token_resource": True,
                }
            ),
        }
        sdk_app = ep.build(identity, refusal=GATEWAY_REFUSAL, **server_kwargs).streamable_http_app(
            streamable_http_path=ep.path, stateless_http=True, json_response=True, transport_security=security
        )
        if ep.gated_tenants is not None:
            # Rebuilt (not wrapped) so the gate runs INSIDE the SDK's authentication middleware, where the verified token is known.
            sdk_app = Starlette(
                routes=list(sdk_app.routes),
                middleware=[
                    *sdk_app.user_middleware,
                    Middleware(TenantGate, server=ep.name, tenant_claim=cfg.tenant_claim, allowed_tenants=ep.gated_tenants),
                ],
                lifespan=sdk_app.router.lifespan_context,
            )
        children.append(sdk_app)
        table[ep.path] = sdk_app
        table[f"/.well-known/oauth-protected-resource{ep.path}"] = sdk_app

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for child in children:  # each owns its session manager's task group; a Mount would not start them
                await stack.enter_async_context(child.router.lifespan_context(child))
            logger.info(
                "mcp_gateway_started",
                extra={"public_url": cfg.public_url, "issuer": cfg.issuer, "endpoints": [e.path for e in endpoints], "ops_tenants": len(cfg.ops_tenants)},
            )
            try:
                yield
            finally:
                await keys.aclose()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(_request: Request) -> JSONResponse:
        # Ready means "can check a token": a replica that cannot reach the signing keys is taken out of rotation by the load balancer
        # rather than answering every caller with a 401.
        if await keys.ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "signing keys unavailable"}, status_code=503)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Mount("/", app=_Dispatch(table)),
        ],
        lifespan=lifespan,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="The MCP gateway: the published MCP servers over Streamable HTTP behind OAuth 2.1.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default loopback; the compose service passes 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    configure_logging()
    configure_telemetry("agent-core-mcp-gateway")  # metrics are PUSHED (OTLP), like the workers': nothing scrapes this process
    try:
        app = create_app()
    except GatewayConfigError as exc:
        sys.exit(f"mcp-gateway: {exc}")
    # log_config=None keeps this repo's JSON logging; the access log is off because a request line names a path and nothing else
    # worth keeping, and the audit line (app/mcp/audit.py) is the per-call record.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
