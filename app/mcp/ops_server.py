"""MCP server exposing read-only ops-domain tools
(`app/domains/ops/tools.py`'s `fetch_metrics_summary`/
`list_recent_incidents`) over MCP — GRAPH_PATTERNS.md pattern 50, the same
story `app/mcp/server.py` tells for `query_employees`, extended to a
second domain.

A SEPARATE server/process from `app/mcp/server.py`'s
`ecorp-structured-data` rather than one more `@mcp.tool()` bolted on —
this app runs one process per domain everywhere else
(`app/job_queue/agent_worker.py`, `AGENT_DOMAIN`), so this keeps that
shape instead of mixing Ecorp's directory and ops data behind one name.

Real use case: an on-call engineer's Claude Desktop/Cursor can pull live
incident status and metrics directly, no chat UI needed.

## Identity over MCP, same seam as app/mcp/server.py

Ops data isn't tenant-scoped (`ctx` here proves "a legitimate caller of
this deployment," not a row filter — see `ops/tools.py`), but
`OPS_POLICY.permit` still fails closed on a missing/malformed ctx. The
identity comes from an `IdentitySource` (`app/mcp/identity.py`) chosen where
the server is built, never from a tool argument the connected assistant could
set. Over stdio it is the launcher's principal under `DEFAULT_TENANT` (the
server refuses to start without a principal, and says why on stderr). Over HTTP
(`app/mcp/gateway.py`) it is the verified token's tenant and principal, and
because this data belongs to the deployment and not to a tenant, the gateway
only lets the operator tenants in (`MCP_GATEWAY_OPS_TENANTS`).

Run with: `make mcp-serve-ops` (stdio transport, same as app/mcp/server.py;
needs MCP_PRINCIPAL).
"""
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from app.core.config import DEFAULT_TENANT
from app.core.logging_config import configure_logging
from app.domains.ops.tools import (
    OPS_POLICY,
    fetch_metrics_summary_impl,
    list_recent_incidents_impl,
)
from app.mcp.audit import audited
from app.mcp.identity import (
    IdentityMissing,
    IdentitySource,
    LauncherIdentityMissing,
    launcher_ctx,
    launcher_source,
)

SERVER_NAME = "ecorp-ops"

NO_IDENTITY_REFUSAL = "Refused: this server was started without an identity (MCP_PRINCIPAL)."

# Both tools only read: Prometheus and the incident log, both this deployment's own. See the same note in app/mcp/server.py.
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(identity: IdentitySource, *, refusal: str = NO_IDENTITY_REFUSAL, **server_kwargs: Any) -> MCPServer:
    """The server, acting under `identity`. `server_kwargs` go to `MCPServer` (the gateway passes its token verifier and auth
    settings); `refusal` is what a call with no identity to act under is told."""
    mcp = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Query Ecorp's operational metrics and incident log. The caller's "
            "identity is fixed outside the conversation; every argument "
            "narrows the result, there is no free-form query."
        ),
        **server_kwargs,
    )

    @mcp.tool(title="Fetch operational metrics summary", annotations=_READ_ONLY)
    @audited(SERVER_NAME, identity)
    async def fetch_metrics_summary() -> str:
        """Fetch this app's current operational metrics (turn error rate,
        latency, tool error rate, moderation blocks, rate limiting, retrieval
        degradation, checkpoint issues) and flag anything past its
        alert-matching threshold. Read-only — pulls from Prometheus, changes
        nothing."""
        try:
            ctx = identity()
        except IdentityMissing:
            return refusal
        if not OPS_POLICY.permit("fetch_metrics", ctx):
            return "Refused: principal is required."
        return await fetch_metrics_summary_impl()

    @mcp.tool(title="List recent incidents", annotations=_READ_ONLY)
    @audited(SERVER_NAME, identity)
    async def list_recent_incidents(status: str | None = None) -> str:
        """List recently logged incidents, most recent first — optionally
        filtered to 'open' or 'resolved'. Read-only — changes nothing."""
        try:
            ctx = identity()
        except IdentityMissing:
            return refusal
        if not OPS_POLICY.permit("list_recent_incidents", ctx):
            return "Refused: principal is required."
        return await list_recent_incidents_impl(status)

    return mcp


# The stdio server: acts for whoever launched the process, under the deployment's own tenant.
mcp = build_server(launcher_source(tenant=DEFAULT_TENANT))


def main() -> None:
    configure_logging()
    try:
        launcher_ctx(tenant=DEFAULT_TENANT)
    except LauncherIdentityMissing as exc:
        sys.exit(f"{SERVER_NAME}: {exc}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
