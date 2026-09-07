"""MCP server exposing read-only ops-domain tools (app/domains/ops/tools.py's
`fetch_metrics_summary`/`list_recent_incidents`) over the Model Context
Protocol — GRAPH_PATTERNS.md pattern 50, the same "expose this app's own
tools to an external MCP client" story app/mcp/server.py already tells for
`query_employees`, extended to a second domain.

A SEPARATE server/process from app/mcp/server.py's `ecorp-structured-data`
rather than one more `@mcp.tool()` bolted onto it — this app already runs
one process per domain everywhere else (app/turns/agent_worker.py,
AGENT_DOMAIN), so an ops-specific MCP server keeps that same shape rather
than mixing Ecorp's employee directory and ops's operational data behind
one name.

Real use case: an on-call engineer's own Claude Desktop/Cursor can pull
live incident status and metrics directly — "is anything currently
flagged as anomalous," "what incidents happened this week" — without
opening this app's chat UI at all.

## Identity over MCP, same seam as app/mcp/server.py

Ops data isn't tenant-scoped (see app/domains/ops/tools.py's own module
docstring: `ctx` here proves "a legitimate caller of this deployment," not
a filter over rows a caller isn't supposed to see) — but `OPS_POLICY.permit`
still fails closed on a missing/malformed ctx, same as every other tool in
this app. So, same demo simplification app/mcp/server.py's own docstring
already discloses for `query_employees`: `principal` is an explicit tool
argument here (not derived from any verified caller identity — an MCP
client has no equivalent of this app's own RunnableConfig channel), just
enough to construct a valid SecurityCtx and satisfy that fail-closed check.
`tenant` is fixed to a constant demo value rather than also being a caller
argument, since nothing this server exposes is ever filtered by it.

Run with: `make mcp-serve-ops` (stdio transport, same as app/mcp/server.py).
"""
from mcp.server.fastmcp import FastMCP

from app.core.config import DEFAULT_TENANT
from app.core.security import SecurityCtx
from app.domains.ops.tools import (
    OPS_POLICY,
    _fetch_metrics_summary_impl,
    _list_recent_incidents_impl,
)

mcp = FastMCP(
    name="ecorp-ops",
    instructions=(
        "Query Ecorp's operational metrics and incident log. "
        "`principal` identifies the caller (see this server's module "
        "docstring for why it's an explicit argument here); every other "
        "argument narrows the result, there is no free-form query."
    ),
)


@mcp.tool()
def fetch_metrics_summary(principal: str) -> str:
    """Fetch this app's current operational metrics (turn error rate,
    latency, tool error rate, moderation blocks, rate limiting, retrieval
    degradation, checkpoint issues) and flag anything past its
    alert-matching threshold. Read-only — pulls from Prometheus, changes
    nothing."""
    ctx: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": principal, "claims": {}}
    if not OPS_POLICY.permit("fetch_metrics", ctx):
        return "Refused: principal is required."
    return _fetch_metrics_summary_impl()


@mcp.tool()
def list_recent_incidents(principal: str, status: str | None = None) -> str:
    """List recently logged incidents, most recent first — optionally
    filtered to 'open' or 'resolved'. Read-only — changes nothing."""
    ctx: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": principal, "claims": {}}
    if not OPS_POLICY.permit("list_recent_incidents", ctx):
        return "Refused: principal is required."
    return _list_recent_incidents_impl(status)


if __name__ == "__main__":
    mcp.run(transport="stdio")
