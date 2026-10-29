"""Who a PUBLISHED MCP server acts for: never an argument the model fills in. Two sources, one rule.

`app/mcp/server.py` and `ops_server.py` used to take `tenant` and `principal` as tool arguments, because MCP
has no `RunnableConfig` and nothing upstream stamps a context. That put the isolation boundary where an
attacker reaches: a desktop assistant fills those arguments itself, so a page it read or a message it was sent
could ask for another tenant's rows and get them. The repo's rule for every other tool is "take
`tenant`/`principal` from `ctx`, never from an argument" (`.claude/rules/side-effect-tools.md`); this module
applies it where a tool is reached without a `ctx`.

An `IdentitySource` is a zero-argument callable returning the `SecurityCtx` a call acts under, or raising
`IdentityMissing`. Tools take one when built (`build_server(identity)` in each server module), so the source
is a choice made at construction, not a mode a tool can be talked out of:
- `launcher_ctx` (stdio, `make mcp-serve`): the identity belongs to whoever STARTS the process (the client's
  `env` block, or `.env`): `MCP_TENANT` and `MCP_PRINCIPAL`. Over stdio the person who owns the machine starts
  the process, which is why the environment is the right place to bind it; it still doesn't authenticate them.
- `token_source` (the gateway, `app/mcp/gateway.py`, specs/017): the identity comes from the verified OAuth
  2.1 access token of THIS request (claims validated by `gateway_auth.py`). A network service's environment
  says nothing about the caller, so it is never consulted; every request is its own identity.

Either fails closed. A stdio `main()` calls `launcher_ctx()` first and exits before serving if the identity is
missing; a tool called without one (the `mcp dev` inspector imports the module without `main()`) returns a
refusal, not an unscoped result. A gateway request whose token lacks a usable tenant and principal is refused
the same way (no valid token at all was already a 401).
"""
from collections.abc import Callable

from mcp.server.auth.middleware.auth_context import get_access_token

from app.core.config import settings
from app.core.security import SecurityCtx, valid_ctx

IdentitySource = Callable[[], SecurityCtx]


class IdentityMissing(Exception):
    """A call has no identity to act under: it must be refused, never run unscoped."""


class LauncherIdentityMissing(IdentityMissing):
    """The process was started without the identity it needs to act for anyone."""


class TokenIdentityMissing(IdentityMissing):
    """The request carries no verified token, or one without a usable tenant and principal."""


def launcher_ctx(*, tenant: str | None = None) -> SecurityCtx:
    """The `SecurityCtx` this server process acts under.

    `tenant` is for a server whose data is not tenant-scoped (the operations server: its metrics and incident log are the
    deployment's, so it passes `DEFAULT_TENANT`); everything else takes it from `MCP_TENANT`. The principal is always
    `MCP_PRINCIPAL`: it is who the audit line and the policy gate name. Raises `LauncherIdentityMissing`, never returns a
    half-filled context.
    """
    ctx: SecurityCtx = {
        "tenant": (settings.mcp_tenant if tenant is None else tenant).strip(),
        "principal": settings.mcp_principal.strip(),
        "claims": {},
    }
    if not valid_ctx(ctx):
        needed = "MCP_PRINCIPAL" if tenant is not None else "MCP_TENANT and MCP_PRINCIPAL"
        raise LauncherIdentityMissing(
            f"{needed} must be set in the environment this server is launched with (the `env` block of the client's "
            "server entry, or .env for `make mcp-serve`): it is who the server acts for, and it is never a tool argument."
        )
    return ctx


def launcher_source(*, tenant: str | None = None) -> IdentitySource:
    """`launcher_ctx` as an `IdentitySource`, with the operations server's fixed tenant bound in."""
    return lambda: launcher_ctx(tenant=tenant)


def _claim(claims: dict, name: str) -> str:
    """A claim as text, or empty. Only a string counts: a list or a number in the tenant claim is a token that was not issued the
    way this service expects, and must not be coerced into something that happens to match a row."""
    value = claims.get(name)
    return value.strip() if isinstance(value, str) else ""


def token_source(*, tenant_claim: str, principal_claim: str) -> IdentitySource:
    """The identity of the request being served, from the claims of its verified access token.

    The claims were validated (signature, issuer, audience, expiry) by `gateway_auth.JwtVerifier` before the request reached a
    tool; this only reads two of them and refuses when either is absent. `get_access_token()` is the request's own: the gateway
    runs the MCP transport statelessly, so no request can read another's.
    """

    def source() -> SecurityCtx:
        token = get_access_token()
        if token is None:
            raise TokenIdentityMissing("this request carries no verified access token")
        claims = token.claims or {}
        ctx: SecurityCtx = {
            "tenant": _claim(claims, tenant_claim),
            "principal": _claim(claims, principal_claim),
            "claims": {},
        }
        if not valid_ctx(ctx):
            raise TokenIdentityMissing(f"the access token has no usable {tenant_claim!r} and {principal_claim!r} claims")
        return ctx

    return source
