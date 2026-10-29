"""Request dependencies shared by every router: who is calling (`get_ctx`) and which domain they mean
(`get_domain`).

## Identity: two modes, chosen by `AUTH_MODE` (specs/018)

`get_ctx` stamps a `SecurityCtx` (app/core/security.py) from one of two sources; which one is a setting, never
an inference.

**`identity` (the default).** The front door (the authlimit service in front of this API) verifies a person's
access token and adds a short-lived signed identity to the request in `X-Internal-Identity`. This mode
verifies it (app/core/internal_identity.py) and builds the ctx from its `tenant` and `sub` alone.
`X-Tenant-Id` and `X-Principal-Id` are IGNORED, so a client sending them gains nothing, and a request with no
valid identity is refused: a 401, or a 503 when the keys that would check it can't be confirmed current
(app/api/identity.py). No default tenant, no fallback to the headers.

**`headers`.** `X-Tenant-Id` and `X-Principal-Id` are trusted as sent, both required (a missing one is a 422
before the handler runs). Safe only where nothing a stranger controls can reach this API: the hermetic tests,
and the API alone on a developer's machine. It is announced loudly (a startup warning, the gauge
`agent_api_auth_mode`, the alert `ApiHeaderAuthMode`) because the mistake it invites is a deployment that
forgot to leave it. In either mode the app owes the right shape at the boundary: never a client-settable body
field, never a default identity, never a ctx the caller wrote.

`valid_ctx` (app/core/security.py) is what every consumer still fails closed on: an identity-mode ctx is valid
by construction (the verifier refuses an empty tenant or subject); a header-mode ctx may carry an empty
string, which the consumers refuse.
"""
from typing import Annotated

from fastapi import Header, HTTPException
from fastapi.exceptions import RequestValidationError

from app.api import identity
from app.core.config import settings
from app.core.internal_identity import Identity
from app.core.security import SecurityCtx
from app.domains.registry import DOMAINS


def _missing_header(name: str) -> dict:
    """The entry FastAPI itself reports for a required header that was not sent (so header mode's 422 is unchanged by being optional here)."""
    return {"type": "missing", "loc": ("header", name), "msg": "Field required", "input": None}


async def get_ctx(
    x_tenant_id: Annotated[str | None, Header(description="Header mode only: the tenant, as sent. Ignored in identity mode.")] = None,
    x_principal_id: Annotated[str | None, Header(description="Header mode only: the principal, as sent. Ignored in identity mode.")] = None,
    x_internal_identity: Annotated[
        str | None, Header(description="Identity mode only: the front door's signed identity. Never send it yourself; the front door overwrites it.")
    ] = None,
) -> SecurityCtx:
    """The caller, by the mode in force. The mode is read per request, so a test (or nothing else) can change it."""
    if settings.auth_mode == "headers":
        # Required in this mode, and FastAPI cannot know that when the other mode does not need them, so the check it would have made is made here
        # and reported in its own words: a request missing either never reaches an endpoint, naming exactly the headers it lacked.
        absent = [name for name, value in (("x-tenant-id", x_tenant_id), ("x-principal-id", x_principal_id)) if value is None]
        if absent:
            raise RequestValidationError([_missing_header(name) for name in absent])
        return {"tenant": x_tenant_id or "", "principal": x_principal_id or "", "claims": {}}

    verdict = await identity.verifier().verify(x_internal_identity)
    if isinstance(verdict, Identity):
        return {"tenant": verdict.tenant, "principal": verdict.principal, "claims": {}}
    raise identity.refusal_for(verdict)


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
