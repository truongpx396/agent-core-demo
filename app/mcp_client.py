"""MCP client: consuming a REMOTE tool catalog as LangChain tools this
app's own graph can bind — the reverse direction from `app/mcp_server.py`
(this app EXPOSING a tool to external clients). Closes the "no support
for consuming a remote MCP tool catalog" gap (GRAPH_PATTERNS.md pattern
28): a `DomainPlugin` (`app/manifest.py`) can now include tools it
doesn't implement itself, sourced from any MCP server reachable over
stdio.

## Capability enforcement for tools this app didn't author (AR-004b)

Every tool in this app declares a capability (`read_only`/`mutating`/
`outward` — `app/tools.py::TOOL_CAPABILITIES`) that `should_continue`
(GRAPH_PATTERNS.md pattern 15) uses to decide whether a tool call needs
human approval before running. A remote MCP tool is code this app
doesn't control, and per the MCP spec, a tool's self-reported
`ToolAnnotations` (`readOnlyHint`, `destructiveHint`, ...) are HINTS, not
verified guarantees — a malicious or just-unmaintained remote server can
claim `readOnlyHint=True` for a tool that deletes data (verified
empirically: this app's own `app/mcp_server.py` doesn't set any
annotations at all, so trusting them would mean silently defaulting to
"unknown" for every tool a naive integration might connect to). Trusting
that claim would let a config-only binding — adding a remote MCP server
as a tool source — hand an ungated mutating/outward action to a run
that's already carrying private-data access and untrusted content,
exactly the "capability budget" hole this app's whole capability-gate
design exists to close.

So: `load_remote_tools`'s `capability_overrides` — supplied by the LOCAL
caller binding this remote server, never read from the remote tool's own
metadata — is the ONLY source of truth for a remote tool's capability.
Any remote tool NOT named in `capability_overrides` defaults to
`"outward"`, the same fail-closed default `app/tools.py::_tool_capability`
already applies to an in-process tool missing from `TOOL_CAPABILITIES` —
an unmaintained or newly-added remote tool is gated, never silently
trusted, by construction.

## One connection per call, by design

Each wrapped tool call opens a fresh stdio connection to the remote
server, calls the tool, and closes it — no persistent session held
across the app's process lifetime. Simpler (no connection-lifecycle/
reconnect logic, nothing to clean up on shutdown) at the cost of
per-call latency — an honest, disclosed tradeoff for a demo, not a
hidden one; a production integration reaching a remote server on every
single turn would likely want a persistent, reconnecting session instead.
"""
import asyncio
import logging

from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


async def _call_remote_tool(params: StdioServerParameters, tool_name: str, kwargs: dict) -> str:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, kwargs)
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
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

    def sync_call(**kwargs) -> str:
        # ToolNode dispatches sync tools from graph.invoke()'s call stack,
        # which has no event loop of its own to reuse.
        return asyncio.run(_call_remote_tool(params, name, kwargs))

    async def async_call(**kwargs) -> str:
        # Used instead of sync_call when the graph runs via
        # astream_events/ainvoke — that path is already inside a running
        # event loop, and asyncio.run() cannot nest inside one.
        return await _call_remote_tool(params, name, kwargs)

    return StructuredTool.from_function(
        func=sync_call,
        coroutine=async_call,
        name=name,
        description=description,
        args_schema=remote_tool.inputSchema,  # a raw JSON Schema dict — langchain_core
        # 0.3's StructuredTool accepts this directly (verified empirically),
        # so the LLM sees the remote tool's actual parameter names/types
        # rather than an opaque **kwargs.
    )


def load_remote_tools(
    command: str,
    args: list[str] | None = None,
    capability_overrides: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[list[StructuredTool], dict[str, str]]:
    """Connects to a remote MCP server (stdio transport), lists its tools,
    and returns `(langchain_tools, tool_capabilities)` — the second dict
    is meant to be merged into a `DomainPlugin.tool_capabilities()`
    mapping (see module docstring: an unlisted remote tool's capability
    defaults to `"outward"`, never inferred from the remote's own
    annotations).
    """
    capability_overrides = capability_overrides or {}
    params = StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd)

    remote_tools = asyncio.run(_list_remote_tools(params))

    langchain_tools = [_wrap_remote_tool(params, t) for t in remote_tools]
    tool_capabilities = {
        t.name: capability_overrides.get(t.name, "outward") for t in remote_tools
    }
    return langchain_tools, tool_capabilities
