"""Who a PUBLISHED MCP server acts for: the launcher's identity, never an argument the model fills in.

`app/mcp/server.py` and `app/mcp/ops_server.py` used to take `tenant` and `principal` as tool arguments, because MCP has no
`RunnableConfig` and nothing upstream stamps a context for it. That put the isolation boundary in the one place an attacker
reaches: a desktop assistant connected to the server fills those arguments in itself, so a page it read, or a message it was
sent, could ask for another tenant's rows and get them. This repo's own rule for every other tool is "take `tenant`/`principal`
from `ctx`, never from an argument the model can set" (`.claude/rules/side-effect-tools.md`); this module is that rule for the
one place a tool is reached without a `ctx`.

The identity now belongs to whoever STARTS the process: the `env` block of the client's server entry (Claude Desktop, Cursor), or
`.env` for `make mcp-serve`. The server reads `MCP_TENANT` and `MCP_PRINCIPAL` (`app/core/config.py`), builds one `SecurityCtx`,
and no tool exposes either name. Over standard input and output the process is started by the person who owns the machine, which
is what makes the environment the right place to bind it; it still does not authenticate that person. A networked transport
would need the identity to come from a verified credential on each connection (OAuth 2.1 resource-server behaviour, with the
token's audience checked), and that is deliberately not built (specs/009 A4, GRAPH_PATTERNS.md pattern 21).

It fails closed twice: `main()` of each server calls `launcher_ctx()` first and exits before serving anything when the identity
is missing, and a tool called without one (the `mcp dev` inspector imports the module without going through `main()`) returns a
refusal instead of an unscoped result.
"""
from app.core.config import settings
from app.core.security import SecurityCtx, valid_ctx


class LauncherIdentityMissing(Exception):
    """The process was started without the identity it needs to act for anyone."""


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
