"""MCP server exposing the structured-data tool (`app/core/sql_store.py`'s `query_employees`) so an external
client (Claude Desktop, another agent, `mcp dev`) can reach it, not just this app's own LLM (GRAPH_PATTERNS.md
pattern 21). Built on `mcp.server.MCPServer`, the 2.x SDK's server class (what `FastMCP` became).

Identity: every other tool reads `SecurityCtx` from `RunnableConfig["configurable"]["ctx"]`, sourced from a
trusted HTTP header (`app/api/deps.py::get_ctx`). MCP has no such channel, so `build_server(identity)` takes
an `IdentitySource` (`app/mcp/identity.py`) and no tool takes either name as an argument. An earlier version
did, so the connected assistant chose the tenant it read: one prompt injection from another tenant's rows. The
policy gate (`DEFAULT_POLICY.permit(...)`) and `query_employees`'s mandatory `WHERE tenant = %s` run as
before; only where the tenant comes from changed.

Two sources: over stdio (`make mcp-serve`) the identity is the launcher's (`MCP_TENANT`, `MCP_PRINCIPAL`),
started by the person who owns the machine, whom nothing authenticates; over HTTP (`app/mcp/gateway.py`,
specs/017) it is the verified token of each request, and the launcher's environment is never consulted.

Run with `make mcp-serve` (stdio; needs MCP_TENANT and MCP_PRINCIPAL).
"""
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from app.bootstrap import wire
from app.core.logging_config import configure_logging
from app.core.security import DEFAULT_POLICY
from app.domains.ecorp.usecases import Department, query_employees_impl
from app.mcp.audit import audited
from app.mcp.identity import (
    IdentityMissing,
    IdentitySource,
    LauncherIdentityMissing,
    launcher_ctx,
    launcher_source,
)

SERVER_NAME = "ecorp-structured-data"

NO_IDENTITY_REFUSAL = "Refused: this server was started without an identity (MCP_TENANT and MCP_PRINCIPAL)."


def build_server(identity: IdentitySource, *, refusal: str = NO_IDENTITY_REFUSAL, **server_kwargs: Any) -> MCPServer:
    """The server, acting under `identity`. `server_kwargs` go to `MCPServer` (the gateway passes its token verifier and auth
    settings); `refusal` is what a call with no identity to act under is told."""
    mcp = MCPServer(
        name=SERVER_NAME,
        instructions=(
            "Query Ecorp's employee directory for the tenant this server acts for. "
            "department/name_contains are the only two optional narrowing filters — "
            "there is no free-form query, and no way to choose a different tenant."
        ),
        **server_kwargs,
    )

    @mcp.tool(
        title="Look up Ecorp employees",
        # A hint to the client, not a guarantee it can rely on: it lets a desktop assistant offer the call without a
        # confirmation prompt. True of all of it: a fixed SELECT, nothing written, nothing outside this deployment's database.
        annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    )
    @audited(SERVER_NAME, identity)
    async def query_employees(
        department: str | None = None,
        name_contains: str | None = None,
    ) -> str:
        """Look up Ecorp employees, optionally filtered by department
        (Engineering, Support, or Sales) or a case-insensitive name substring.
        Results are always scoped to the tenant this server acts for, which no
        argument can change — a fixed, parameterized query, never SQL text the
        caller supplies."""
        try:
            ctx = identity()
        except IdentityMissing:
            return refusal
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

    return mcp


# The stdio server: acts for whoever launched the process.
mcp = build_server(launcher_source())


def main() -> None:
    wire()
    # JSON lines to STDERR (stdout is the protocol channel), and no serving at all without an identity.
    configure_logging()
    try:
        launcher_ctx()
    except LauncherIdentityMissing as exc:
        sys.exit(f"{SERVER_NAME}: {exc}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
