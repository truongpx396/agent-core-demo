"""MCP client: consuming a REMOTE tool catalog as LangChain tools this
app's graph can bind — the reverse direction from `app/mcp/server.py`
(this app exposing a tool). Closes GRAPH_PATTERNS.md pattern 28: a
`DomainPlugin` can include tools it doesn't implement itself, sourced from
any MCP server reachable over stdio.

A remote server is someone else's code, and everything it sends is input: its tool
names, descriptions and schemas are shown to the model, its results go into the
model's context. Four rules follow, each one a decision made HERE rather than left to
the server.

## Capability is the caller's, never the remote's (AR-004b)

Every tool declares a capability (`read_only`/`mutating`/`outward` —
`app/agent/tools/builtin.py::SHARED_TOOL_CAPABILITIES`) that `should_continue` uses to
gate human approval. A remote MCP tool's self-reported `ToolAnnotations`
(`readOnlyHint`, etc.) are HINTS per the MCP spec, not verified
guarantees — a malicious or unmaintained server could claim
`readOnlyHint=True` for a tool that deletes data. So `load_remote_tools`'s
`capability_overrides` — supplied by the LOCAL caller, never read from the
remote's own metadata — is the ONLY source of truth for a remote tool's
capability. Any tool not named in `capability_overrides` defaults to
`"outward"`, same fail-closed default as `tool_capability` for an
in-process tool missing from `TOOL_CAPABILITIES`.

## What the model is told is pinned, not trusted

A tool's description is text the model obeys, which makes it the place a tool-poisoning
instruction lives, and a server can change it after the first look (a dependency
upgrade nobody read is the ordinary way). `pinned` maps each tool the caller expects to a
`tool_fingerprint` of its name, description and input schema, exactly what the model
would be shown. A tool not in the map is dropped (least privilege: the sandbox bridge
offers 19 tools and the app uses 5), a tool whose fingerprint differs is dropped and
counted (`agent_mcp_tool_pin_failures_total{reason=changed}`), and one that has gone is
counted as `missing`. Without `pinned` every tool the server offers is wrapped as it is
described: trust on first use, so a caller that loads a server it did not write should pin.

## What comes back is rendered, scrubbed and bounded

Text blocks are joined; an embedded text resource is included; a link is shown as one;
an image or audio block, which the model cannot read, becomes a one-line note instead of
vanishing; a result with no text but with structured content is that JSON. The result is
credential-scrubbed BEFORE it is cut (a secret that straddles the cut would otherwise
escape the pattern that finds it whole) and then bounded to
`MCP_TOOL_OUTPUT_MAX_CHARS` (`app/core/output_limits.py`). A caller that parses the
reply itself and bounds what goes on to the model passes `cap_output=False`: cutting a
JSON document in the middle leaves it unparseable.

## One connection per server, kept open

Every call on an event loop shares one long-lived stdio connection to a given server
(`app/mcp/connection.py`): the process starts once, not once per call. A connection found
dead is replaced on the next call, a call that may have reached the server is never sent a
second time, and `close_all_connections()` ends them at shutdown. That module's docstring
has the measured behaviour and the reasons.
"""
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from langchain_core.tools import StructuredTool
from mcp import types
from mcp.client.stdio import StdioServerParameters

from app.core import metrics
from app.core.config import MCP_TOOL_OUTPUT_MAX_CHARS
from app.core.output_limits import bound_output
from app.core.scrubbing import scrub
from app.mcp.connection import connection_for

logger = logging.getLogger(__name__)

# What a failed remote call starts with, so a caller that parses replies (the sandbox) can tell a failure from a payload.
REMOTE_ERROR_PREFIX = "Remote tool error:"


@dataclass(frozen=True)
class _Handling:
    """How one remote's replies are treated; fixed when its catalog is loaded."""

    dependency: str  # names the remote on metrics and logs
    cap_output: bool


def tool_fingerprint(name: str, description: str | None, input_schema: Mapping) -> str:
    """A stable hash of what the model is shown about one tool: its name, description and input schema. Annotations are not
    in it: they are never shown to the model and never trusted (see the module docstring). The key stays `inputSchema`, MCP's name
    for it on the wire, whatever the SDK calls the attribute: the pinned hashes are over this exact form."""
    canonical = json.dumps(
        {"name": name, "description": description or "", "inputSchema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_result(result: types.CallToolResult, handling: _Handling) -> str:
    """One remote reply as the text the model gets. See the module docstring for each rule."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.EmbeddedResource) and isinstance(block.resource, types.TextResourceContents):
            parts.append(block.resource.text)
        elif isinstance(block, types.ResourceLink):
            parts.append(f"[resource: {block.uri}]")
        else:
            parts.append(f"[{block.type} content omitted: this agent cannot read it]")
    text = "\n".join(parts)
    if not text and result.structured_content is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False, default=str)
    # Scrubbed here too, not just in-process tools' run_with_timeout — a remote
    # server this app doesn't own is at least as likely to echo a
    # credential-shaped value back (pattern 32). Before the cut, never after.
    text = scrub(text)
    if handling.cap_output:
        text = bound_output(text, MCP_TOOL_OUTPUT_MAX_CHARS, dependency=handling.dependency)
    return f"{REMOTE_ERROR_PREFIX} {text}" if result.is_error else text


async def _call_remote_tool(params: StdioServerParameters, tool_name: str, kwargs: dict, handling: _Handling) -> str:
    result = await connection_for(params, handling.dependency).call_tool(tool_name, kwargs)
    return render_result(result, handling)


async def _list_remote_tools(params: StdioServerParameters, handling: _Handling):
    return await connection_for(params, handling.dependency).list_tools()


def _wrap_remote_tool(params: StdioServerParameters, remote_tool, handling: _Handling) -> StructuredTool:
    name = remote_tool.name
    description = remote_tool.description or f"Remote MCP tool {name!r}."

    async def async_call(**kwargs) -> str:
        return await _call_remote_tool(params, name, kwargs, handling)

    return StructuredTool.from_function(
        coroutine=async_call,
        name=name,
        description=description,
        args_schema=remote_tool.input_schema,  # a raw JSON Schema dict —
        # langchain_core 0.3's StructuredTool accepts this directly, so the
        # LLM sees the remote tool's real parameter names/types, not **kwargs.
    )


def _apply_pins(remote_tools: list, pinned: Mapping[str, str], dependency: str) -> list:
    """Keep only the tools whose name AND fingerprint are the pinned ones; count and log the rest that matter."""
    kept = []
    offered: set[str] = set()
    for tool in remote_tools:
        offered.add(tool.name)
        expected = pinned.get(tool.name)
        if expected is None:
            # Not a failure: the server offers more than this app uses, and the unused tools are the point of pinning.
            logger.debug("mcp_tool_not_pinned", extra={"dependency": dependency, "tool": tool.name})
            continue
        actual = tool_fingerprint(tool.name, tool.description, tool.input_schema)
        if actual != expected:
            metrics.agent_mcp_tool_pin_failures_total.labels(dependency=dependency, reason="changed").inc()
            logger.warning(
                "mcp_tool_pin_changed",
                extra={"dependency": dependency, "tool": tool.name, "pinned": expected, "offered": actual},
            )
            continue
        kept.append(tool)
    for name in sorted(set(pinned) - offered):
        metrics.agent_mcp_tool_pin_failures_total.labels(dependency=dependency, reason="missing").inc()
        logger.warning("mcp_tool_pin_missing", extra={"dependency": dependency, "tool": name})
    return kept


async def load_remote_tools(
    command: str,
    args: list[str] | None = None,
    capability_overrides: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    *,
    pinned: Mapping[str, str] | None = None,
    dependency: str = "mcp",
    cap_output: bool = True,
) -> tuple[list[StructuredTool], dict[str, str]]:
    """Connects to a remote MCP server (stdio transport), lists its tools,
    and returns `(langchain_tools, tool_capabilities)` — the second dict
    merges into a `DomainPlugin.tool_capabilities()` mapping (unlisted
    tools default to `"outward"`, see module docstring).

    `pinned` (name -> `tool_fingerprint`) restricts the catalog to exactly those tools, unchanged; `dependency` names the
    remote on the metrics and log lines this module emits; `cap_output=False` is for a caller that parses replies itself
    (see the module docstring for each).

    `async def`, awaiting `_list_remote_tools` directly — the `mcp` SDK
    client is async-native and this function's one real caller
    (`app/domains/sandbox_tools.py::load_sandbox_tools`) is `async def`
    too, so there's no sync/async boundary to bridge. Every wrapped remote
    tool is `coroutine`-only for the same reason.
    """
    capability_overrides = capability_overrides or {}
    params = StdioServerParameters(command=command, args=args or [], env=env, cwd=cwd)
    handling = _Handling(dependency=dependency, cap_output=cap_output)

    remote_tools = await _list_remote_tools(params, handling)
    if pinned is not None:
        remote_tools = _apply_pins(remote_tools, pinned, dependency)

    langchain_tools = [_wrap_remote_tool(params, t, handling) for t in remote_tools]
    tool_capabilities = {
        t.name: capability_overrides.get(t.name, "outward") for t in remote_tools
    }
    return langchain_tools, tool_capabilities
