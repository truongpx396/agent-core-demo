"""The code sandbox: an isolated, disposable container the ops, support and sales domains can run commands, Python and file reads and writes in.

The domains' sandbox tools (`app/domains/sandbox_session.py`) own the session semantics: one sandbox per tenant and conversation, found again by
its owner tags, every reply parsed as JSON, output scrubbed and bounded. The adapter (`app/adapters/mcp/sandbox_tools.py`) owns reaching the
sandbox: `opensandbox-mcp`, a stdio MCP server bridging to a containerized `opensandbox-server`, consumed through the pinned MCP client
(`app/mcp/client.py`).

`load` is fail-soft by contract: a missing or hung bridge degrades to an empty catalog and a counted warning, never an exception, so a domain
that merges the tools in still builds with every other tool intact. Every tool it returns is capped at "outward" (the mandatory-approval gate is
the real boundary; sandbox capability annotations are never trusted).
"""
from __future__ import annotations

from typing import Protocol

from langchain_core.tools import BaseTool

from app.ports.slot import Slot

# The label on every metric about this bridge, the same as its circuit breaker's.
SANDBOX_DEPENDENCY = "opensandbox_mcp"

# What a remote MCP tool's error reply starts with: the MCP adapter writes it, the sandbox session reads it to tell an error from an answer.
REMOTE_ERROR_PREFIX = "Remote tool error:"


class SandboxCatalog(Protocol):
    async def load(self) -> tuple[list[BaseTool], dict[str, str]]:
        """The sandbox's tools and their capability overrides. Never raises; `([], {})` when the sandbox is unavailable."""
        ...


sandbox_catalog: Slot[SandboxCatalog] = Slot("sandbox_catalog", SandboxCatalog)
