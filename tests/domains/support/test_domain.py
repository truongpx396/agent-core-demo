"""Proves the support-copilot domain (app/domains/support/) is a REAL,
load-bearing domain on the same seam tests/agent/test_manifest.py's
widget-support example already proved out (GRAPH_PATTERNS.md pattern 23)
— not just config ceremony. Same shape: build the graph with THIS domain's
manifest+plugin, show its ToolNode only knows this domain's tools (the
literal meaning of "sandboxed" — see app/domains/support/domain.py), and
show a mutating tool call pauses for human_approval and, once approved,
actually runs.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps, build_graph
from app.domains.support import store
from app.domains.support.domain import SUPPORT_DOMAIN_PLUGIN, SUPPORT_MANIFEST
from tests.conftest import TEST_CTX


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, row=(1,)):
        self._row = row
        self.calls: list[tuple[str, list]] = []

    def execute(self, sql, params):
        self.calls.append((sql, list(params)))
        return _FakeCursor(self._row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _config():
    return {"configurable": {"thread_id": "support-thread", "ctx": TEST_CTX}}


def _fake_llm_returning(*responses):
    return GenericFakeChatModel(messages=iter(responses))


def _build(llm=None):
    return build_graph(GraphDeps(llm=llm), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)


class TestSandboxing:
    """The literal meaning of "sandboxed... knowledge base + ticket
    system" access: this domain's ToolNode knows exactly the KB tools plus
    its own five ticket tools (plus its own, domain-scoped run_subagent —
    see TestDomainScopedSubagent below), and nothing else Acme's TOOLS
    list has."""

    def test_tool_node_only_knows_the_support_domains_tools(self):
        g = build_graph(GraphDeps(llm=None), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)
        assert set(g.nodes["tools"].bound.tools_by_name) == {
            "search_docs",
            "skill_search",
            "use_skill",
            "ask_clarification",
            "create_ticket",
            "check_ticket_status",
            "escalate_to_human",
            "list_my_tickets",
            "add_ticket_comment",
            "run_subagent",
        }

    def test_acme_only_tools_are_absent(self):
        g = _build()
        names = set(g.nodes["tools"].bound.tools_by_name)
        for excluded in ("calculator", "add_note", "remember", "query_employees"):
            assert excluded not in names


class TestDomainScopedSubagent:
    """run_subagent is present, but it's this domain's OWN closure-built
    tool (app.agent.tools.make_domain_subagent_tool), never Acme's literal
    module-level object — and its menu offers only the bundled subagent(s)
    declared `domains: [support]` (subagents/ticket-researcher/AGENT.md),
    never Acme's own `researcher`."""

    def test_is_not_the_acme_level_run_subagent_object(self):
        from app.agent.tools import run_subagent as acme_run_subagent

        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        assert domain_run_subagent is not acme_run_subagent

    def test_menu_offers_only_the_support_domains_own_subagent(self):
        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        schema = domain_run_subagent.args_schema.model_json_schema()
        enum_def = next(iter(schema["$defs"].values()))
        assert enum_def["enum"] == ["ticket-researcher"]

    def test_never_pauses_it_is_read_only(self, monkeypatch):
        from app.agent import tools as agent_tools_module

        monkeypatch.setattr(
            agent_tools_module,
            "_run_subagent_impl",
            lambda *a, **k: "found it",
        )
        llm = _fake_llm_returning(
            _tool_call(
                "run_subagent",
                {"subagent_name": "ticket-researcher", "task": "what's ticket 7 about?"},
            ),
            AIMessage(content="Here's what the subagent found out for you."),
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="look into ticket 7 for me")]}, config=_config()
        )
        assert not g.get_state(_config()).next  # never paused


class TestMandatoryApprovalGate:
    def test_create_ticket_pauses_for_approval(self):
        llm = _fake_llm_returning(
            _tool_call("create_ticket", {"subject": "Login broken", "description": "Can't log in"})
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="I can't log in")]}, config=_config()
        )
        assert g.get_state(_config()).next  # paused, not finished

    def test_approving_runs_create_ticket_and_finishes(self, monkeypatch):
        fake_conn = _FakeConnection(row=(7,))
        monkeypatch.setattr(store, "get_connection", lambda: fake_conn)

        llm = _fake_llm_returning(
            _tool_call(
                "create_ticket",
                {"subject": "Login broken", "description": "Can't log in", "priority": "high"},
            ),
            AIMessage(content="I've opened ticket #7 for you."),
        )
        g = build_graph(GraphDeps(llm=llm), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)
        g.invoke({"messages": [HumanMessage(content="I can't log in")]}, config=_config())
        result = g.invoke(Command(resume=True), config=_config())

        assert not g.get_state(_config()).next  # finished, not paused
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert any("Ticket #7 opened" in m.content for m in tool_messages)
        assert result["messages"][-1].content == "I've opened ticket #7 for you."

    def test_check_ticket_status_is_read_only_and_never_pauses(self, monkeypatch):
        monkeypatch.setattr(
            store,
            "get_ticket",
            lambda tenant, ticket_id: {
                "id": ticket_id,
                "status": "open",
                "priority": "normal",
                "subject": "Login broken",
                "escalation_reason": None,
            },
        )
        llm = _fake_llm_returning(
            _tool_call("check_ticket_status", {"ticket_id": 7}),
            AIMessage(content="Ticket #7 is still open."),
        )
        g = build_graph(GraphDeps(llm=llm), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)
        result = g.invoke(
            {"messages": [HumanMessage(content="what's the status of ticket 7?")]},
            config=_config(),
        )
        assert not g.get_state(_config()).next  # never paused
        assert result["messages"][-1].content == "Ticket #7 is still open."

    def test_list_my_tickets_is_read_only_and_never_pauses(self, monkeypatch):
        monkeypatch.setattr(
            store, "list_tickets_for_requester", lambda tenant, requester, limit=10: []
        )
        llm = _fake_llm_returning(
            _tool_call("list_my_tickets", {}),
            AIMessage(content="You have no open tickets."),
        )
        g = build_graph(GraphDeps(llm=llm), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)
        result = g.invoke(
            {"messages": [HumanMessage(content="what tickets have I opened?")]},
            config=_config(),
        )
        assert not g.get_state(_config()).next  # never paused
        assert result["messages"][-1].content == "You have no open tickets."

    def test_add_ticket_comment_pauses_for_approval_and_runs_once_approved(self, monkeypatch):
        monkeypatch.setattr(store, "add_comment", lambda tenant, ticket_id, comment: True)

        llm = _fake_llm_returning(
            _tool_call("add_ticket_comment", {"ticket_id": 7, "comment": "still happening"}),
            AIMessage(content="Added that to ticket #7."),
        )
        g = build_graph(GraphDeps(llm=llm), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN)
        g.invoke(
            {"messages": [HumanMessage(content="it's still happening on ticket 7")]},
            config=_config(),
        )
        assert g.get_state(_config()).next  # paused, not finished

        result = g.invoke(Command(resume=True), config=_config())
        assert not g.get_state(_config()).next  # finished, not paused
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert any("Added your follow-up to ticket #7" in m.content for m in tool_messages)


def test_escalate_to_human_notifies_the_team_channel(monkeypatch):
    """escalate_to_human's own side effect (app/domains/support/tools.py)
    — a unit-level check, not through the graph, so it doesn't also need a
    fake LLM/tool-call round trip just to reach one function call."""
    from app.domains import notify
    from app.domains.support.tools import _escalate_to_human_impl

    monkeypatch.setattr(store, "escalate_ticket", lambda tenant, ticket_id, reason: True)
    posted = {}
    monkeypatch.setattr(
        notify, "post_to_team_channel", lambda channel, message: posted.setdefault(channel, message)
    )

    result = _escalate_to_human_impl(7, "needs a refund", TEST_CTX)

    assert "escalated" in result.lower()
    assert "support-escalations" in posted
    assert "needs a refund" in posted["support-escalations"]
