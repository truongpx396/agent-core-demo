"""MCP server exposing the structured-data tool (app/sql_store.py's
query_employees) over the Model Context Protocol, so an external MCP client
(Claude Desktop, another agent, `mcp dev`) can reach it — not just this
app's own in-process LLM (GRAPH_PATTERNS.md pattern 21).

Uses `mcp.server.fastmcp.FastMCP` from the standard `mcp` SDK, pinned to
`mcp==1.29.0` in requirements.txt: the current 2.x release removed
`mcp.server.fastmcp` in a rewrite (verified empirically — ModuleNotFoundError
on 2.0.0), so this app deliberately stays on the last stable 1.x line that
still has the documented, standard FastMCP API.

## Identity over MCP: explicit arguments, not a RunnableConfig

Every other tool in this app (app/tools.py) reads `SecurityCtx` out of
`RunnableConfig["configurable"]["ctx"]` — a channel that only exists because
this app's own LangGraph runtime puts it there (app/agent.py's `_config`),
itself sourced from a trusted HTTP header (app/api.py's `get_ctx`) that a
gateway is assumed to set. An MCP client is a wholly different trust
boundary with no such channel: MCP has no equivalent of `configurable`, and
nothing upstream of this process is stamping a tenant/principal for it.

This demo's simplification: `tenant`/`principal` are explicit tool
arguments, checked against the same `DEFAULT_POLICY.permit(...)` fail-closed
gate app/tools.py's tools use, then passed straight through to
`sql_store.query_employees`'s mandatory `WHERE tenant = %s` — so the
isolation boundary this whole app is built around still holds even here.
What this demo does NOT do is authenticate the *caller* — a production MCP
server serving multiple tenants would derive tenant/principal from the
connecting client's own verified identity (MCP's OAuth-based auth support,
or a proxy ahead of it), never accept them as caller-supplied arguments,
for the same reason app/api.py's docstring gives for its own trusted-header
seam: nothing downstream should trust a value the caller could simply type
in. That auth wiring is out of scope for this local demo; the query-layer
tenant scoping it hands off to is the actual content being demonstrated.

Run with: `make mcp-serve` (stdio transport — the standard way an MCP
client like Claude Desktop launches a local server as a subprocess).
"""
from mcp.server.fastmcp import FastMCP

from app.security import DEFAULT_POLICY, SecurityCtx
from app.tools import Department, _query_employees_impl

mcp = FastMCP(
    name="acme-structured-data",
    instructions=(
        "Query Acme Corp's employee directory. tenant/principal identify "
        "the caller (see this server's module docstring for why they're "
        "explicit arguments here); department/name_contains are the only "
        "two optional narrowing filters — there is no free-form query."
    ),
)


@mcp.tool()
def query_employees(
    tenant: str,
    principal: str,
    department: str | None = None,
    name_contains: str | None = None,
) -> str:
    """Look up Acme Corp employees, optionally filtered by department
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

    return _query_employees_impl(dept, name_contains, ctx)


if __name__ == "__main__":
    mcp.run(transport="stdio")
