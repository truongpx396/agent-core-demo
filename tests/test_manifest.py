"""Proves `build_graph()` is genuinely domain-agnostic (GRAPH_PATTERNS.md
pattern 23) — not just that `AgentManifest`/`DomainPlugin` exist as types
nobody exercises. A SECOND domain plugin below has a completely different
tool, prompt, and Policy from the Acme domain `build_graph()` defaults to.
It runs green end-to-end through the exact same, unmodified graph topology
`app/graph.py` already ships — no `if domain == "..."` anywhere in that
file, no domain-specific node. That's the concrete meaning of "swap a
manifest+plugin, never fork the graph."
"""
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import Command

from app.graph import GraphDeps, build_graph
from app.manifest import AgentManifest, DEFAULT_DOMAIN_PLUGIN, DEFAULT_MANIFEST
from app.security import Policy, SecurityCtx, valid_ctx

WIDGET_CTX: SecurityCtx = {"tenant": "widgetco", "principal": "support-agent", "claims": {}}


class _AllowAllPolicy:
    """Deliberately NOT `TenantIsolationPolicy` — a different Policy
    *class*, not just a differently-configured instance of the same one,
    so this is a real test of "swap the policy," not a relabeled tenant.
    Permits any action for any structurally-valid ctx; `lower` is unused
    by this domain's one tool (it has no vector store to pre-filter) but
    still implemented to honestly satisfy the Policy protocol.
    """

    def permit(self, action: str, ctx: SecurityCtx) -> bool:
        return valid_ctx(ctx)

    def lower(self, ctx: SecurityCtx, target: str):
        from qdrant_client.models import Filter

        return Filter()


_WIDGET_POLICY: Policy = _AllowAllPolicy()


@tool
def open_ticket(config: RunnableConfig, summary: str) -> str:
    """Open a support ticket for a widgetco customer."""
    ctx = (config.get("configurable") or {}).get("ctx")
    if not _WIDGET_POLICY.permit("open_ticket", ctx):
        return "Refused: no valid security context."
    return f"Ticket opened: {summary}"


@dataclass
class _WidgetSupportDomainPlugin:
    """The "thin domain plugin (code)" half — one tool, its capability,
    and the Policy backing it. Nothing here touches app/graph.py."""

    def tools(self) -> list:
        return [open_ticket]

    def tool_capabilities(self) -> dict[str, str]:
        return {"open_ticket": "mutating"}

    def policy(self) -> Policy:
        return _WIDGET_POLICY


WIDGET_DOMAIN = _WidgetSupportDomainPlugin()
WIDGET_MANIFEST = AgentManifest(
    name="widget-support",
    system_prompt="You are a widget-support agent. Use open_ticket to file customer issues.",
    allowed_tools=("open_ticket",),
)


def _tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _config():
    return {"configurable": {"thread_id": "widget-thread", "ctx": WIDGET_CTX}}


class TestDefaultsToTheAcmeDomain:
    """build_graph() with no manifest/domain argument at all must keep
    behaving exactly as it did before pattern 23 existed — the whole point
    of a default is that nobody who doesn't care about multi-domain has to
    change anything."""

    def test_default_manifest_is_the_acme_domain(self):
        assert DEFAULT_MANIFEST.name == "acme"

    def test_default_domain_plugin_exposes_acmes_existing_tools(self):
        names = {t.name for t in DEFAULT_DOMAIN_PLUGIN.tools()}
        assert "search_docs" in names
        assert "query_employees" in names

    def test_compiled_graph_carries_the_default_manifest(self):
        g = build_graph(GraphDeps(llm=None))
        assert g.manifest is DEFAULT_MANIFEST


class TestSecondDomainProvesReuse:
    def test_widget_domains_tool_node_only_knows_its_own_tool(self):
        """The most direct proof TOOLS was actually swapped, not just
        that the LLM binding changed: ToolNode itself only has
        `open_ticket` registered — Acme's search_docs/calculator/add_note/
        remember/query_employees are entirely absent."""
        g = build_graph(
            GraphDeps(llm=None), manifest=WIDGET_MANIFEST, domain=WIDGET_DOMAIN
        )
        assert set(g.nodes["tools"].bound.tools_by_name) == {"open_ticket"}

    def test_compiled_graph_carries_the_widget_manifest(self):
        g = build_graph(
            GraphDeps(llm=None), manifest=WIDGET_MANIFEST, domain=WIDGET_DOMAIN
        )
        assert g.manifest is WIDGET_MANIFEST
        assert g.manifest.system_prompt != DEFAULT_MANIFEST.system_prompt

    def test_mandatory_capability_gate_pauses_for_the_widget_domains_own_tool(self):
        """The load-bearing behavioral proof: should_continue's mandatory
        human_approval gate (GRAPH_PATTERNS.md pattern 15) — unmodified,
        same function every Acme turn uses — correctly treats
        open_ticket as mutating using THIS domain's tool_capabilities,
        not Acme's TOOL_CAPABILITIES (which has never heard of
        open_ticket and would default it to "outward" — also gated,
        which would make this test pass for the wrong reason, so the
        assertion below checks the specific "mutating" label instead)."""
        from app import metrics

        before = metrics.agent_capability_gate_total.labels(capability="mutating")._value.get()

        llm = _fake_llm_returning(
            _tool_call("open_ticket", {"summary": "printer on fire"})
        )
        g = build_graph(
            GraphDeps(llm=llm), manifest=WIDGET_MANIFEST, domain=WIDGET_DOMAIN
        )
        g.invoke(
            {"messages": [HumanMessage(content="my printer is on fire")]},
            config=_config(),
        )

        assert g.get_state(_config()).next  # paused, not finished
        assert (
            metrics.agent_capability_gate_total.labels(capability="mutating")._value.get()
            == before + 1
        )

    def test_approving_runs_the_widget_domains_tool_and_finishes(self):
        llm = _fake_llm_returning(
            _tool_call("open_ticket", {"summary": "printer on fire"}),
            AIMessage(content="I've opened a ticket for your printer issue."),
        )
        g = build_graph(
            GraphDeps(llm=llm), manifest=WIDGET_MANIFEST, domain=WIDGET_DOMAIN
        )
        g.invoke(
            {"messages": [HumanMessage(content="my printer is on fire")]},
            config=_config(),
        )
        result = g.invoke(Command(resume=True), config=_config())

        assert not g.get_state(_config()).next  # finished, not paused
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert any("Ticket opened" in m.content for m in tool_messages)
        assert result["messages"][-1].content == "I've opened a ticket for your printer issue."

    def test_acmes_tool_capabilities_are_unaffected_by_the_widget_domain_existing(self):
        """Building a graph for one domain must not mutate any shared,
        module-level state the OTHER domain reads — proves the two
        domains' capability mappings are genuinely independent dicts, not
        accidentally the same object with one mutated in place."""
        from app.tools import TOOL_CAPABILITIES

        build_graph(GraphDeps(llm=None), manifest=WIDGET_MANIFEST, domain=WIDGET_DOMAIN)

        assert "open_ticket" not in TOOL_CAPABILITIES
        assert TOOL_CAPABILITIES["search_docs"] == "read_only"


def _fake_llm_returning(*responses):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    return GenericFakeChatModel(messages=iter(responses))
