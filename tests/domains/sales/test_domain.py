"""Proves the sales/CRM-concierge domain (app/domains/sales/) on the same
seam tests/agent/test_manifest.py's widget-support example and
tests/domains/support/test_domain.py already proved out — this domain's
ToolNode only knows its own tools, and a mutating tool call pauses for
human_approval and, once approved, actually runs.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from app.domains.sales import store
from app.domains.sales.domain import SALES_DOMAIN_PLUGIN, SALES_MANIFEST
from tests.conftest import TEST_CTX

_CORE_SALES_TOOLS = {
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
    "enrich_lead_from_website",
    "run_subagent",
}

_SANDBOX_TOOLS = {"run_command_in_sandbox", "run_python_in_sandbox", "read_sandbox_file", "write_sandbox_file"}


def _tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _config():
    return {"configurable": {"thread_id": "sales-thread", "ctx": TEST_CTX}}


def _fake_llm_returning(*responses):
    return GenericFakeChatModel(messages=iter(responses))


def _build(llm=None):
    return build_graph(GraphDeps(llm=llm), manifest=SALES_MANIFEST, domain=SALES_DOMAIN_PLUGIN)


class TestSandboxing:
    """Not a full exact-set check — app/domains/sales/tools.py also adds
    its own four narrow sandbox tools (app/domains/sandbox_session.py,
    GRAPH_PATTERNS.md pattern 50), always present as a fixed set of four
    regardless of whether opensandbox-mcp happens to be reachable when
    this test runs — reachability became a per-call concern, not a
    presence concern, after a real, live bug (see
    sandbox_session.load_raw_sandbox_tools's own docstring) — same fix
    app/domains/ops/test_domain.py and tests/domains/support/test_domain.py
    already needed for the same reason."""

    def test_tool_node_knows_at_least_the_core_sales_domains_tools(self):
        g = _build()
        assert _CORE_SALES_TOOLS <= set(g.nodes["tools"].bound.tools_by_name)

    def test_sandbox_tools_are_always_present_as_a_fixed_set(self):
        g = _build()
        assert _SANDBOX_TOOLS <= set(g.nodes["tools"].bound.tools_by_name)

    def test_every_sandbox_tool_present_is_declared_outward(self):
        g = _build()
        present = _SANDBOX_TOOLS & set(g.nodes["tools"].bound.tools_by_name)
        capabilities = SALES_DOMAIN_PLUGIN.tool_capabilities()
        for name in present:
            assert capabilities.get(name) == "outward", name

    def test_ecorp_only_tools_are_absent(self):
        g = _build()
        names = set(g.nodes["tools"].bound.tools_by_name)
        for excluded in ("calculator", "add_note", "remember", "query_employees"):
            assert excluded not in names


class TestDomainScopedSubagent:
    """Same proof as tests/domains/support/test_domain.py's own
    TestDomainScopedSubagent: run_subagent is this domain's OWN
    closure-built tool, and its menu offers only the bundled subagent(s)
    declared `domains: [sales]` (subagents/lead-researcher/AGENT.md)."""

    def test_is_not_the_ecorp_level_run_subagent_object(self):
        from app.agent.subagent_tools import run_subagent as ecorp_run_subagent

        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        assert domain_run_subagent is not ecorp_run_subagent

    def test_menu_offers_only_the_sales_domains_own_subagent(self):
        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        schema = domain_run_subagent.args_schema.model_json_schema()
        enum_def = next(iter(schema["$defs"].values()))
        assert enum_def["enum"] == ["lead-researcher"]

    def test_never_pauses_it_is_read_only(self, monkeypatch):
        from app.agent import subagent_tools as agent_tools_module

        monkeypatch.setattr(
            agent_tools_module,
            "_run_subagent_impl",
            lambda *a, **k: agent_tools_module.SubagentResult("found it", 0, 0.0),
        )
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


def test_enrich_lead_from_website_pauses_for_approval_as_an_outward_tool():
    llm = _fake_llm_returning(
        _tool_call(
            "enrich_lead_from_website",
            {"contact": "jordan@example.com", "url": "https://ecorp-lead.example.com"},
        )
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="jordan's company site is ecorp-lead.example.com")]},
        config=_config(),
    )
    assert g.get_state(_config()).next  # paused, not finished


def test_approving_enrich_lead_from_website_runs_it_and_finishes(monkeypatch):
    from app.domains.sales import tools as sales_tools

    monkeypatch.setattr(
        store, "get_lead", lambda tenant, contact: {"name": "Jordan", "contact": contact}
    )
    monkeypatch.setattr(store, "append_lead_note", lambda tenant, contact, note: True)
    monkeypatch.setattr(
        sales_tools, "render_url_to_markdown", lambda url: "Ecorp Lead Co — mid-market SaaS."
    )

    llm = _fake_llm_returning(
        _tool_call(
            "enrich_lead_from_website",
            {"contact": "jordan@example.com", "url": "https://ecorp-lead.example.com"},
        ),
        AIMessage(content="Added research on Jordan's company to their notes."),
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="jordan's company site is ecorp-lead.example.com")]},
        config=_config(),
    )
    result = g.invoke(Command(resume=True), config=_config())

    assert not g.get_state(_config()).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Ecorp Lead Co" in m.content for m in tool_messages)


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


def test_run_command_in_sandbox_pauses_for_approval_and_runs_once_approved(monkeypatch):
    """Same shape as app/domains/ops/test_domain.py's own version of this
    test — the sandbox trio is always present now, but its impl still
    calls load_raw_sandbox_tools() fresh on every real call, so stub that
    out too, not just run_command_in_sandbox_impl."""
    from app.domains.sales import tools as sales_tools

    monkeypatch.setattr(sales_tools.sandbox_session, "load_raw_sandbox_tools", lambda: {"command_run": object()})
    monkeypatch.setattr(
        sales_tools.sandbox_session,
        "run_command_in_sandbox_impl",
        lambda command, thread_id, raw: "exit code: 0\nstdout:\n3-year total: 142575.00\n",
    )

    llm = _fake_llm_returning(
        _tool_call("run_command_in_sandbox", {"command": "python3 -c \"...\""}),
        AIMessage(content="The 3-year deal value is $142,575.00."),
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="what's this 3-year deal worth with the discount")]},
        config=_config(),
    )
    assert g.get_state(_config()).next  # paused, not finished

    result = g.invoke(Command(resume=True), config=_config())
    assert not g.get_state(_config()).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("142575.00" in m.content for m in tool_messages)


def test_run_python_in_sandbox_pauses_for_approval_and_runs_once_approved(monkeypatch):
    """Same shape as run_command_in_sandbox's own version of this test —
    run_python_in_sandbox exists specifically so a deal-economics script
    (dict literals, f-strings) can go straight into a real script
    parameter instead of fighting shell quoting via `python -c '...'`
    (a real, repeatedly-observed failure mode)."""
    from app.domains.sales import tools as sales_tools

    monkeypatch.setattr(sales_tools.sandbox_session, "load_raw_sandbox_tools", lambda: {"command_run": object()})
    monkeypatch.setattr(
        sales_tools.sandbox_session,
        "run_python_in_sandbox_impl",
        lambda script, thread_id, raw: "exit code: 0\nstdout:\n3-year total: 142575.00\n",
    )

    llm = _fake_llm_returning(
        _tool_call("run_python_in_sandbox", {"script": "print('3-year total: 142575.00')"}),
        AIMessage(content="The 3-year deal value is $142,575.00."),
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="what's this 3-year deal worth with the discount")]},
        config=_config(),
    )
    assert g.get_state(_config()).next  # paused, not finished

    result = g.invoke(Command(resume=True), config=_config())
    assert not g.get_state(_config()).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("142575.00" in m.content for m in tool_messages)
