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
`OPS_POLICY.permit` still fails closed on a missing/malformed ctx.
`principal` is an explicit tool argument (an MCP client has no
`RunnableConfig` channel) just enough to construct a valid `SecurityCtx`;
`tenant` is fixed to a constant demo value since nothing here is filtered
by it — same simplification `app/mcp/server.py` discloses for
`query_employees`.

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
async def fetch_metrics_summary(principal: str) -> str:
    """Fetch this app's current operational metrics (turn error rate,
    latency, tool error rate, moderation blocks, rate limiting, retrieval
    degradation, checkpoint issues) and flag anything past its
    alert-matching threshold. Read-only — pulls from Prometheus, changes
    nothing."""
    ctx: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": principal, "claims": {}}
    if not OPS_POLICY.permit("fetch_metrics", ctx):
        return "Refused: principal is required."
    return await _fetch_metrics_summary_impl()


@mcp.tool()
async def list_recent_incidents(principal: str, status: str | None = None) -> str:
    """List recently logged incidents, most recent first — optionally
    filtered to 'open' or 'resolved'. Read-only — changes nothing."""
    ctx: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": principal, "claims": {}}
    if not OPS_POLICY.permit("list_recent_incidents", ctx):
        return "Refused: principal is required."
    return await _list_recent_incidents_impl(status)


if __name__ == "__main__":
    mcp.run(transport="stdio")
