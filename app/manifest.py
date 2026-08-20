"""Config-first multi-domain composition layer (GRAPH_PATTERNS.md pattern
23): the ONE `app/graph.py` StateGraph topology — every node, every edge,
every routing function — is adapted to a new domain by swapping an
`AgentManifest` (config) plus a `DomainPlugin` (a thin bundle of that
domain's own tools/capabilities/policy), never by forking `build_graph()`
or branching on a domain name anywhere inside it.

Two deliberately different halves:

- `AgentManifest` — everything that could plausibly live in a YAML/JSON
  file without touching code: a name, the system prompt, and which of the
  plugin's tools this deployment actually exposes to its LLM.
- `DomainPlugin` — the CODE a manifest can't express: the actual tool
  implementations, their capability declarations (read_only/mutating/
  outward — see `app/tools.py::TOOL_CAPABILITIES`), and the `Policy` those
  tools enforce internally. A plugin's tools are expected to already carry
  their own tenant/principal scoping the same way `app/tools.py`'s do
  (`_ctx_or_refuse`, `Policy.lower(...)`) — `build_graph()` never reaches
  into a Policy itself, because nothing in `app/graph.py`'s own node/edge
  code calls one today (see pattern 17): Policy enforcement lives at the
  tool-implementation boundary, not the graph-topology boundary, and this
  module doesn't invent a new consumption point just to exercise
  `DomainPlugin.policy()` — it exists so a plugin can assert/expose which
  Policy backs it (useful for tests and docs), not because build_graph
  threads it anywhere.

`DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN` wrap this app's existing Acme
setup completely unchanged — proof that today's single-domain app was
always just the *default* domain, not a special case `build_graph()` has
to keep working around. See `tests/test_manifest.py` for a second,
completely different domain (different tools, prompt, and Policy) proving
the same unmodified graph runs green end-to-end with it.

A note on the import direction: this module imports `app.graph.SYSTEM_PROMPT`
(for `DEFAULT_MANIFEST`) at module load time. `app/graph.py` imports this
module back — but only *inside* `build_graph()`'s function body, never at
`app/graph.py`'s own module level. `build_graph()` is only ever *called*
after `app.graph` has finished importing, so by the time that deferred
import runs, `SYSTEM_PROMPT` already exists — the cycle never actually
closes in either import order. Don't move that import to `app/graph.py`'s
top without re-checking this.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.graph import SYSTEM_PROMPT
from app.security import DEFAULT_POLICY, Policy
from app.tools import TOOL_CAPABILITIES, TOOLS


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
        see `app/tools.py::TOOL_CAPABILITIES`'s docstring for the mandatory
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
    (see `app/graph.py`'s `State` docstring).
    """

    name: str
    system_prompt: str
    # Subset of the plugin's tool names to actually expose to this
    # deployment's LLM. Empty/omitted means "everything the plugin
    # offers" — a plugin author doesn't have to enumerate every tool name
    # twice just to expose all of them.
    allowed_tools: tuple[str, ...] = ()


@dataclass
class _AcmeDomainPlugin:
    """Wraps this app's existing tools/capabilities/policy, unchanged."""

    def tools(self) -> list:
        return list(TOOLS)

    def tool_capabilities(self) -> dict[str, str]:
        return dict(TOOL_CAPABILITIES)

    def policy(self) -> Policy:
        return DEFAULT_POLICY


DEFAULT_DOMAIN_PLUGIN: DomainPlugin = _AcmeDomainPlugin()

DEFAULT_MANIFEST = AgentManifest(
    name="acme",
    system_prompt=SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in TOOLS),
)
