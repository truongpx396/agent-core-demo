"""MCP client: consuming a REMOTE tool catalog as LangChain tools this
app's graph can bind — the reverse direction from `app/mcp/server.py`
(this app exposing a tool). Closes GRAPH_PATTERNS.md pattern 28: a
`DomainPlugin` can include tools it doesn't implement itself, sourced from
any MCP server reachable over stdio.

## Capability enforcement for tools this app didn't author (AR-004b)

Every tool declares a capability (`read_only`/`mutating`/`outward` —
`app/agent/tools.py::TOOL_CAPABILITIES`) that `should_continue` uses to
gate human approval. A remote MCP tool's self-reported `ToolAnnotations`
(`readOnlyHint`, etc.) are HINTS per the MCP spec, not verified
guarantees — a malicious or unmaintained server could claim
`readOnlyHint=True` for a tool that deletes data. So `load_remote_tools`'s
`capability_overrides` — supplied by the LOCAL caller, never read from the
remote's own metadata — is the ONLY source of truth for a remote tool's
capability. Any tool not named in `capability_overrides` defaults to
`"outward"`, same fail-closed default as `_tool_capability` for an
in-process tool missing from `TOOL_CAPABILITIES`.

## One connection per call, by design

Each wrapped tool call opens a fresh stdio connection, calls the tool, and
closes it — no persistent session. Simpler at the cost of per-call
latency; a production integration would likely want a persistent,
reconnecting session instead.
"""
import logging

from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from app.core.scrubbing import scrub

logger = logging.getLogger(__name__)


async def _call_remote_tool(params: StdioServerParameters, tool_name: str, kwargs: dict) -> str:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, kwargs)
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            # Scrubbed here too, not just in-process tools' _run_with_timeout —
            # a remote server this app doesn't own is at least as likely to
            # echo a credential-shaped value back (pattern 32).
            text = scrub(text)
            if result.isError:
                return f"Remote tool error: {text}"
            return text


async def _list_remote_tools(params: StdioServerParameters):
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return result.tools


def _wrap_remote_tool(params: StdioServerParameters, remote_tool) -> StructuredTool:
    name = remote_tool.name
    description = remote_tool.description or f"Remote MCP tool {name!r}."

    async def async_call(**kwargs) -> str:
        return await _call_remote_tool(params, name, kwargs)

    return StructuredTool.from_function(
        coroutine=async_call,
        name=name,
        description=description,
        args_schema=remote_tool.inputSchema,  # a raw JSON Schema dict —
        # langchain_core 0.3's StructuredTool accepts this directly, so the
        # LLM sees the remote tool's real parameter names/types, not **kwargs.
    )


async def load_remote_tools(
    command: str,
    args: list[str] | None = None,
    capability_overrides: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[list[StructuredTool], dict[str, str]]:
    """Connects to a remote MCP server (stdio transport), lists its tools,
    and returns `(langchain_tools, tool_capabilities)` — the second dict
    merges into a `DomainPlugin.tool_capabilities()` mapping (unlisted
    tools default to `"outward"`, see module docstring).

    `async def`, awaiting `_list_remote_tools` directly — the `mcp` SDK
    client is async-native and this function's one real caller
    (`app/domains/sandbox_tools.py::load_sandbox_tools`) is `async def`
    too, so there's no sync/async boundary to bridge. Every wrapped remote
    tool is `coroutine`-only for the same reason.
    """
    capability_overrides = capability_overrides or {}
    params = StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd)

    remote_tools = await _list_remote_tools(params)

    langchain_tools = [_wrap_remote_tool(params, t) for t in remote_tools]
    tool_capabilities = {
        t.name: capability_overrides.get(t.name, "outward") for t in remote_tools
    }
    return langchain_tools, tool_capabilities
