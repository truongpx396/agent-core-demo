"""Proves the sales/CRM-concierge domain (app/domains/sales/) on the same
seam tests/agent/test_manifest.py's widget-support example and
tests/domains/support/test_domain.py already proved out — this domain's
ToolNode only knows its own tools, and a mutating tool call pauses for
human_approval and, once approved, actually runs.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps, build_graph
from app.domains.sales import store
from app.domains.sales.domain import SALES_DOMAIN_PLUGIN, SALES_MANIFEST
from tests.conftest import TEST_CTX


def _tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _config():
    return {"configurable": {"thread_id": "sales-thread", "ctx": TEST_CTX}}


def _fake_llm_returning(*responses):
    return GenericFakeChatModel(messages=iter(responses))


def _build(llm=None):
    return build_graph(GraphDeps(llm=llm), manifest=SALES_MANIFEST, domain=SALES_DOMAIN_PLUGIN)


class TestSandboxing:
    def test_tool_node_only_knows_the_sales_domains_tools(self):
        g = _build()
        assert set(g.nodes["tools"].bound.tools_by_name) == {
            "search_docs",
            "skill_search",
            "use_skill",
            "ask_clarification",
            "log_lead_interaction",
            "schedule_followup",
            "package_lead_brief",
            "handoff_to_human",
            "list_pending_followups",
            "mark_lead_lost",
            "run_subagent",
        }

    def test_acme_only_tools_are_absent(self):
        g = _build()
        names = set(g.nodes["tools"].bound.tools_by_name)
        for excluded in ("calculator", "add_note", "remember", "query_employees"):
            assert excluded not in names


class TestDomainScopedSubagent:
    """Same proof as tests/domains/support/test_domain.py's own
    TestDomainScopedSubagent: run_subagent is this domain's OWN
    closure-built tool, and its menu offers only the bundled subagent(s)
    declared `domains: [sales]` (subagents/lead-researcher/AGENT.md)."""

    def test_is_not_the_acme_level_run_subagent_object(self):
        from app.agent.tools import run_subagent as acme_run_subagent

        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        assert domain_run_subagent is not acme_run_subagent

    def test_menu_offers_only_the_sales_domains_own_subagent(self):
        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        schema = domain_run_subagent.args_schema.model_json_schema()
        enum_def = next(iter(schema["$defs"].values()))
        assert enum_def["enum"] == ["lead-researcher"]

    def test_never_pauses_it_is_read_only(self, monkeypatch):
        from app.agent import tools as agent_tools_module

        monkeypatch.setattr(agent_tools_module, "_run_subagent_impl", lambda *a, **k: "found it")
        llm = _fake_llm_returning(
            _tool_call(
                "run_subagent",
                {"subagent_name": "lead-researcher", "task": "what's jordan's status?"},
            ),
            AIMessage(content="Here's what the subagent found out for you."),
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="look into jordan for me")]}, config=_config()
        )
        assert not g.get_state(_config()).next  # never paused


class TestMandatoryApprovalGate:
    def test_log_lead_interaction_pauses_for_approval(self):
        llm = _fake_llm_returning(
            _tool_call(
                "log_lead_interaction",
                {"name": "Jordan", "contact": "jordan@example.com", "notes": "asked about pricing"},
            )
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="Hi, what does this cost?")]}, config=_config()
        )
        assert g.get_state(_config()).next  # paused, not finished

    def test_approving_runs_log_lead_interaction_and_finishes(self, monkeypatch):
        monkeypatch.setattr(store, "find_or_create_lead", lambda tenant, name, contact, note: 3)

        llm = _fake_llm_returning(
            _tool_call(
                "log_lead_interaction",
                {"name": "Jordan", "contact": "jordan@example.com", "notes": "asked about pricing"},
            ),
            AIMessage(content="Got it, I've logged that."),
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="Hi, what does this cost?")]}, config=_config()
        )
        result = g.invoke(Command(resume=True), config=_config())

        assert not g.get_state(_config()).next  # finished, not paused
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert any("Logged interaction for lead #3" in m.content for m in tool_messages)


class TestListPendingFollowupsAndMarkLeadLost:
    def test_list_pending_followups_is_read_only_and_never_pauses(self, monkeypatch):
        monkeypatch.setattr(store, "list_pending_followups", lambda tenant, contact=None: [])
        llm = _fake_llm_returning(
            _tool_call("list_pending_followups", {}),
            AIMessage(content="No pending follow-ups."),
        )
        g = _build(llm)
        result = g.invoke(
            {"messages": [HumanMessage(content="what follow-ups are coming up?")]},
            config=_config(),
        )
        assert not g.get_state(_config()).next  # never paused
        assert result["messages"][-1].content == "No pending follow-ups."

    def test_mark_lead_lost_pauses_for_approval_and_runs_once_approved(self, monkeypatch):
        monkeypatch.setattr(store, "mark_lead_lost", lambda tenant, contact, reason: True)

        llm = _fake_llm_returning(
            _tool_call(
                "mark_lead_lost",
                {"contact": "jordan@example.com", "reason": "went with a competitor"},
            ),
            AIMessage(content="Marked that lead lost."),
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="jordan went with a competitor")]},
            config=_config(),
        )
        assert g.get_state(_config()).next  # paused, not finished

        result = g.invoke(Command(resume=True), config=_config())
        assert not g.get_state(_config()).next  # finished, not paused
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert any("marked lost" in m.content.lower() for m in tool_messages)


def test_handoff_to_human_notifies_the_team_channel(monkeypatch):
    """handoff_to_human's own side effect (app/domains/sales/tools.py) —
    unit-level, same shape as
    tests/domains/support/test_domain.py::test_escalate_to_human_notifies_the_team_channel."""
    from app.domains import notify
    from app.domains.sales.tools import _handoff_to_human_impl

    monkeypatch.setattr(store, "set_lead_status", lambda tenant, contact, status: True)
    posted = {}
    monkeypatch.setattr(
        notify, "post_to_team_channel", lambda channel, message: posted.setdefault(channel, message)
    )

    result = _handoff_to_human_impl(
        "jordan@example.com", "Asked for pricing and a demo", "ready to buy", TEST_CTX
    )

    assert "handed off" in result.lower()
    assert "sales-handoffs" in posted
    assert "ready to buy" in posted["sales-handoffs"]
