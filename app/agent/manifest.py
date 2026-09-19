"""Config-first multi-domain composition layer (pattern 23): the ONE
`app/agent/graph.py` StateGraph topology is adapted to a new domain by
swapping an `AgentManifest` (config) plus a `DomainPlugin` (tools/
capabilities/policy), never by forking `build_graph()` or branching on a
domain name.

- `AgentManifest` — the YAML/JSON-able part: name, system prompt, which
  of the plugin's tools this deployment exposes.
- `DomainPlugin` — the CODE a manifest can't express: tool
  implementations, capability declarations (read_only/mutating/outward,
  see `tools.py::TOOL_CAPABILITIES`), and the `Policy` they enforce
  internally. `build_graph()` never calls `.policy()` itself — enforcement
  lives at the tool-implementation boundary (pattern 17); it exists so a
  plugin can assert/expose its own Policy.

`DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN` wrap the existing Ecorp setup
unchanged — proof the single-domain app was always just the default
domain. See `tests/agent/test_manifest.py` for a second domain proving the
same graph runs with different tools/prompt/Policy.

Import direction: this module imports `graph.SYSTEM_PROMPT` at load time;
`graph.py` imports this module back only INSIDE `build_graph()`'s body
(never at its own module level), so the cycle never closes. Don't move
that import to `graph.py`'s top without re-checking this.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agent.graph import SYSTEM_PROMPT
from app.agent.tools import TOOL_CAPABILITIES, TOOLS
from app.core.security import DEFAULT_POLICY, Policy


class DomainPlugin(Protocol):
    """The CODE half of a domain. See this module's docstring for why
    `policy()` exists but is never called by `build_graph()` itself."""

    def tools(self) -> list:
        """This domain's full tool universe (LangChain `@tool`-decorated
        callables) — `AgentManifest.allowed_tools` picks the subset a
        given deployment actually exposes."""
        ...

    def tool_capabilities(self) -> dict[str, str]:
        """Maps each tool name to `"read_only" | "mutating" | "outward"` —
        see `app/agent/tools.py::TOOL_CAPABILITIES`'s docstring for the mandatory
        human_approval gate this feeds (`should_continue`, unchanged
        regardless of domain)."""
        ...

    def policy(self) -> Policy:
        """This domain's `Policy` — informational (see module docstring):
        the plugin's own `tools()` are expected to already enforce it."""
        ...


@dataclass(frozen=True)
class AgentManifest:
    """The CONFIG half of a domain. Frozen so a manifest can't be mutated
    out from under a graph already built with it — the same "stamped once,
    read-only from there on" discipline `State["ctx"]` already follows
    (see `app/agent/graph.py`'s `State` docstring).
    """

    name: str
    system_prompt: str
    # Subset of the plugin's tool names to actually expose to this
    # deployment's LLM. Empty/omitted means "everything the plugin
    # offers" — a plugin author doesn't have to enumerate every tool name
    # twice just to expose all of them.
    allowed_tools: tuple[str, ...] = ()


@dataclass
class _EcorpDomainPlugin:
    """Wraps this app's existing tools/capabilities/policy, unchanged."""

    def tools(self) -> list:
        return list(TOOLS)

    def tool_capabilities(self) -> dict[str, str]:
        return dict(TOOL_CAPABILITIES)

    def policy(self) -> Policy:
        return DEFAULT_POLICY


DEFAULT_DOMAIN_PLUGIN: DomainPlugin = _EcorpDomainPlugin()

DEFAULT_MANIFEST = AgentManifest(
    name="ecorp",
    system_prompt=SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in TOOLS),
)
