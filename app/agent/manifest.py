"""Config-first multi-domain composition layer (pattern 23): the ONE StateGraph topology
(`app/agent/graph/build.py`) is adapted to a domain by swapping an `AgentManifest` (config)
plus a `DomainPlugin` (tools/capabilities/policy), never by forking `build_graph()` or
branching on a domain name.

- `AgentManifest` — the YAML/JSON-able part: name, system prompt, which of the plugin's tools
  this deployment exposes.
- `DomainPlugin` — the CODE a manifest can't express: tool implementations, capability
  declarations (read_only/mutating/outward, see `app/agent/tools/builtin.py::SHARED_TOOL_CAPABILITIES`
  and each domain's `tools.py`), and the `Policy` they enforce internally. `build_graph()` never
  calls `.policy()` itself — enforcement lives at the tool-implementation boundary (pattern 17);
  it exists so a plugin can assert/expose its own Policy.

This module is a leaf: it defines the two types and nothing else. The concrete domains are in
`app/domains/<name>/domain.py` and are looked up by name in `app/domains/registry.py`; Ecorp is
one of them, with no special standing in the graph. See `tests/agent/test_manifest.py` for a second
domain proving the same graph runs with different tools/prompt/Policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.core.security import Policy


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
        see `app/agent/tools/builtin.py::SHARED_TOOL_CAPABILITIES`'s comment for the mandatory
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
    (see `app/agent/graph/state.py`'s `State` docstring).
    """

    name: str
    system_prompt: str
    # Subset of the plugin's tool names to actually expose to this
    # deployment's LLM. Empty/omitted means "everything the plugin
    # offers" — a plugin author doesn't have to enumerate every tool name
    # twice just to expose all of them.
    allowed_tools: tuple[str, ...] = ()
