"""Request dependencies shared by every router: who is calling (`get_ctx`) and
which domain they mean (`get_domain`).

## Identity: trusted headers, NOT authentication (app/core/security.py)

`get_ctx` reads `X-Tenant-Id`/`X-Principal-Id` and stamps a `SecurityCtx`.
This is the seam a real auth middleware plugs into, not authentication
itself — nothing verifies a password/JWT/session, and nothing stops a
client sending any header value. That's fine only because production sits
behind a gateway that authenticates the caller and sets these headers
itself, stripping client-supplied copies first (like `X-Forwarded-*`).
What this app owes is the correct shape at the boundary: required headers,
fail closed (422) if absent, never a client-settable body field, never a
default identity — so real auth later is a gateway config change, not a
rewrite here.
"""
from fastapi import Header, HTTPException

from app.core.security import SecurityCtx
from app.domains.registry import DOMAINS


async def get_ctx(
    x_tenant_id: str = Header(..., description="Trusted-layer tenant id."),
    x_principal_id: str = Header(..., description="Trusted-layer principal id."),
) -> SecurityCtx:
    """Required headers, so a request missing either never reaches an
    endpoint (FastAPI returns 422 before the handler runs) — fail-closed
    lives in the *shape* of the dependency, not a runtime check here."""
    return {"tenant": x_tenant_id, "principal": x_principal_id, "claims": {}}


async def get_domain(
    x_domain: str = Header(
        "ecorp",
        description=(
            "Which domain (app/domains/registry.py) this turn runs against. "
            "Read by every chat endpoint below (all of them queued)."
        ),
    ),
) -> str:
    """Unlike `get_ctx`'s headers, this one defaults rather than fails
    closed — an absent `X-Domain` is the normal case (every caller before
    this existed), so it behaves as before: Ecorp. An UNKNOWN domain is
    still fail-loud (422 here, vs. resolve_domain's startup crash) —
    without this check a typo'd domain would publish onto a requests stream
    no worker pool reads, hanging silently until the caller gives up."""
    if x_domain not in DOMAINS:
        raise HTTPException(
            422, f"Unknown X-Domain {x_domain!r} — must be one of: {', '.join(sorted(DOMAINS))}"
        )
    return x_domain
