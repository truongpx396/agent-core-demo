"""MCP server exposing the structured-data tool (`app/agent/sql_store.py`'s
`query_employees`) over MCP, so an external client (Claude Desktop,
another agent, `mcp dev`) can reach it — not just this app's own in-process
LLM (GRAPH_PATTERNS.md pattern 21).

Pinned to `mcp==1.29.0`: the 2.x release removed `mcp.server.fastmcp` in a
rewrite (ModuleNotFoundError on 2.0.0), so this stays on the last stable
1.x line with the documented FastMCP API.

## Identity over MCP: explicit arguments, not a RunnableConfig

Every other tool (`app/agent/tools.py`) reads `SecurityCtx` from
`RunnableConfig["configurable"]["ctx"]`, sourced from a trusted HTTP header
(`app/api/main.py::get_ctx`). MCP has no equivalent channel — nothing
upstream stamps a tenant/principal for it. This demo's simplification:
`tenant`/`principal` are explicit tool arguments, checked against the same
`DEFAULT_POLICY.permit(...)` fail-closed gate, then passed straight through
to `sql_store.query_employees`'s mandatory `WHERE tenant = %s` — the
isolation boundary still holds. What this demo does NOT do is authenticate
the *caller*: a production server would derive tenant/principal from the
connecting client's verified identity (MCP's OAuth support, or a proxy
ahead of it), never accept them as caller-supplied arguments. That auth
wiring is out of scope here; the query-layer tenant scoping is the actual
content being demonstrated.

Run with: `make mcp-serve` (stdio transport).
"""
from mcp.server.fastmcp import FastMCP

from app.agent.tools import Department, _query_employees_impl
from app.core.security import DEFAULT_POLICY, SecurityCtx

mcp = FastMCP(
    name="ecorp-structured-data",
    instructions=(
        "Query Ecorp's employee directory. tenant/principal identify "
        "the caller (see this server's module docstring for why they're "
        "explicit arguments here); department/name_contains are the only "
        "two optional narrowing filters — there is no free-form query."
    ),
)


@mcp.tool()
async def query_employees(
    tenant: str,
    principal: str,
    department: str | None = None,
    name_contains: str | None = None,
) -> str:
    """Look up Ecorp employees, optionally filtered by department
    (Engineering, Support, or Sales) or a case-insensitive name substring.
    `tenant`/`principal` identify the caller and scope every result — a
    fixed, parameterized query, never SQL text the caller supplies."""
    ctx: SecurityCtx = {"tenant": tenant, "principal": principal, "claims": {}}
    if not DEFAULT_POLICY.permit("query_structured_data", ctx):
        return "Refused: tenant and principal are required."

    dept: Department | None = None
    if department is not None:
        try:
            dept = Department(department)
        except ValueError:
            valid = ", ".join(d.value for d in Department)
            return f"Invalid department {department!r}. Valid values: {valid}."

    return await _query_employees_impl(dept, name_contains, ctx)


if __name__ == "__main__":
    mcp.run(transport="stdio")
