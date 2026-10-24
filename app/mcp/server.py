"""MCP server exposing the structured-data tool (`app/core/sql_store.py`'s
`query_employees`) over MCP, so an external client (Claude Desktop,
another agent, `mcp dev`) can reach it — not just this app's own in-process
LLM (GRAPH_PATTERNS.md pattern 21).

Built on `mcp.server.MCPServer`, the 2.x SDK's server class (what `FastMCP` became there).

## Identity over MCP: the launcher's, never an argument

Every other tool (`app/agent/tools/builtin.py`) reads `SecurityCtx` from
`RunnableConfig["configurable"]["ctx"]`, sourced from a trusted HTTP header
(`app/api/deps.py::get_ctx`). MCP has no equivalent channel — nothing
upstream stamps a tenant/principal for it. So the server binds ONE identity
when it is started (`MCP_TENANT`, `MCP_PRINCIPAL`, see `app/mcp/identity.py`)
and no tool takes either as an argument. An earlier version did, which meant
the connected assistant chose the tenant it read: one prompt injection away
from another tenant's rows. The policy gate (`DEFAULT_POLICY.permit(...)`) and
`sql_store.query_employees`'s mandatory `WHERE tenant = %s` still run exactly
as before; only where the tenant comes from changed.

What this still does NOT do is authenticate the person who starts the
process. Over stdio that is whoever owns the machine; a networked transport
would have to derive the identity from each connection's verified credential
(OAuth 2.1 resource-server behaviour, audience-checked tokens), never from the
environment or an argument. That is deliberately not built (specs/009 A4).

Run with: `make mcp-serve` (stdio transport; needs MCP_TENANT and MCP_PRINCIPAL).
"""
import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from app.core.logging_config import configure_logging
from app.core.security import DEFAULT_POLICY
from app.domains.ecorp.store import Department, query_employees_impl
from app.mcp.audit import audited
from app.mcp.identity import LauncherIdentityMissing, launcher_ctx

SERVER_NAME = "ecorp-structured-data"

NO_IDENTITY_REFUSAL = "Refused: this server was started without an identity (MCP_TENANT and MCP_PRINCIPAL)."

mcp = MCPServer(
    name=SERVER_NAME,
    instructions=(
        "Query Ecorp's employee directory for the tenant this server was "
        "started for. department/name_contains are the only two optional "
        "narrowing filters — there is no free-form query, and no way to "
        "choose a different tenant."
    ),
)


@mcp.tool(
    title="Look up Ecorp employees",
    # A hint to the client, not a guarantee it can rely on: it lets a desktop assistant offer the call without a
    # confirmation prompt. True of all of it: a fixed SELECT, nothing written, nothing outside this deployment's database.
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
)
@audited(SERVER_NAME)
async def query_employees(
    department: str | None = None,
    name_contains: str | None = None,
) -> str:
    """Look up Ecorp employees, optionally filtered by department
    (Engineering, Support, or Sales) or a case-insensitive name substring.
    Results are always scoped to the tenant this server was started for —
    a fixed, parameterized query, never SQL text the caller supplies."""
    try:
        ctx = launcher_ctx()
    except LauncherIdentityMissing:
        return NO_IDENTITY_REFUSAL
    if not DEFAULT_POLICY.permit("query_structured_data", ctx):
        return "Refused: tenant and principal are required."

    dept: Department | None = None
    if department is not None:
        try:
            dept = Department(department)
        except ValueError:
            valid = ", ".join(d.value for d in Department)
            return f"Invalid department {department!r}. Valid values: {valid}."

    return await query_employees_impl(dept, name_contains, ctx)


def main() -> None:
    # JSON lines to STDERR (stdout is the protocol channel), and no serving at all without an identity.
    configure_logging()
    try:
        launcher_ctx()
    except LauncherIdentityMissing as exc:
        sys.exit(f"{SERVER_NAME}: {exc}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
