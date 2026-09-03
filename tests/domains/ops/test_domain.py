"""Proves the ops-bot domain (app/domains/ops/) on the same seam as
tests/domains/support/test_domain.py / tests/domains/sales/test_domain.py
— this domain's ToolNode only knows its own tools, and its `outward`
tool (post_to_team_channel — this repo's first real use of that
capability, see GRAPH_PATTERNS.md pattern 47) is gated exactly like a
mutating one.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps, build_graph
from app.domains import notify
from app.domains.ops.domain import OPS_DOMAIN_PLUGIN, OPS_MANIFEST
from tests.conftest import TEST_CTX


def _tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _config():
    return {"configurable": {"thread_id": "ops-thread", "ctx": TEST_CTX}}


def _fake_llm_returning(*responses):
    return GenericFakeChatModel(messages=iter(responses))


def _build(llm=None):
    return build_graph(GraphDeps(llm=llm), manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)


def test_tool_node_only_knows_the_ops_domains_tools():
    g = _build()
    assert set(g.nodes["tools"].bound.tools_by_name) == {
        "fetch_metrics_summary",
        "post_to_team_channel",
        "ask_clarification",
        "skill_search",
        "use_skill",
        "log_incident",
        "list_recent_incidents",
        "resolve_incident",
        "run_subagent",
    }


class TestDomainScopedSubagent:
    """Same proof as tests/domains/support/test_domain.py's own
    TestDomainScopedSubagent: run_subagent is this domain's OWN
    closure-built tool, and its menu offers only the bundled subagent(s)
    declared `domains: [ops]` (subagents/metrics-researcher/AGENT.md)."""

    def test_is_not_the_acme_level_run_subagent_object(self):
        from app.agent.tools import run_subagent as acme_run_subagent

        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        assert domain_run_subagent is not acme_run_subagent

    def test_menu_offers_only_the_ops_domains_own_subagent(self):
        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        schema = domain_run_subagent.args_schema.model_json_schema()
        enum_def = next(iter(schema["$defs"].values()))
        assert enum_def["enum"] == ["metrics-researcher"]

    def test_never_pauses_it_is_read_only(self, monkeypatch):
        from app.agent import tools as agent_tools_module

        monkeypatch.setattr(agent_tools_module, "_run_subagent_impl", lambda *a, **k: "found it")
        llm = _fake_llm_returning(
            _tool_call(
                "run_subagent",
                {"subagent_name": "metrics-researcher", "task": "has latency spiked before?"},
            ),
            AIMessage(content="Here's what the subagent found out for you."),
        )
        g = _build(llm)
        g.invoke(
            {"messages": [HumanMessage(content="has this happened before?")]}, config=_config()
        )
        assert not g.get_state(_config()).next  # never paused


def test_fetch_metrics_summary_is_read_only_and_never_pauses(monkeypatch):
    from app.domains.ops import metrics_client

    monkeypatch.setattr(metrics_client, "fetch_readings", lambda: {})
    llm = _fake_llm_returning(
        _tool_call("fetch_metrics_summary", {}),
        AIMessage(content="Everything looks normal."),
    )
    g = _build(llm)
    result = g.invoke(
        {"messages": [HumanMessage(content="is everything ok?")]}, config=_config()
    )
    assert not g.get_state(_config()).next  # never paused
    assert result["messages"][-1].content == "Everything looks normal."


def test_post_to_team_channel_pauses_for_approval_as_an_outward_tool():
    llm = _fake_llm_returning(
        _tool_call("post_to_team_channel", {"channel": "ops-digest", "message": "all clear"})
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="post an update to the team")]}, config=_config()
    )
    assert g.get_state(_config()).next  # paused, not finished


def test_list_recent_incidents_is_read_only_and_never_pauses(monkeypatch):
    from app.domains.ops import store

    monkeypatch.setattr(store, "list_recent_incidents", lambda limit=10, status=None: [])
    llm = _fake_llm_returning(
        _tool_call("list_recent_incidents", {}),
        AIMessage(content="No incidents on record."),
    )
    g = _build(llm)
    result = g.invoke(
        {"messages": [HumanMessage(content="has this happened before?")]}, config=_config()
    )
    assert not g.get_state(_config()).next  # never paused
    assert result["messages"][-1].content == "No incidents on record."


def test_log_incident_pauses_for_approval_and_runs_once_approved(monkeypatch):
    from app.domains.ops import store

    monkeypatch.setattr(store, "log_incident", lambda opened_by, summary, detail: 3)

    llm = _fake_llm_returning(
        _tool_call("log_incident", {"summary": "latency spike", "detail": "p95 at 45s"}),
        AIMessage(content="Logged incident #3."),
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="latency looks bad, log it")]}, config=_config()
    )
    assert g.get_state(_config()).next  # paused, not finished

    result = g.invoke(Command(resume=True), config=_config())
    assert not g.get_state(_config()).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Incident #3 logged" in m.content for m in tool_messages)


def test_resolve_incident_pauses_for_approval_and_runs_once_approved(monkeypatch):
    from app.domains.ops import store

    monkeypatch.setattr(store, "resolve_incident", lambda incident_id, resolution: True)

    llm = _fake_llm_returning(
        _tool_call("resolve_incident", {"incident_id": 3, "resolution": "restarted the worker"}),
        AIMessage(content="Resolved incident #3."),
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="incident 3 is fixed now")]}, config=_config()
    )
    assert g.get_state(_config()).next  # paused, not finished

    result = g.invoke(Command(resume=True), config=_config())
    assert not g.get_state(_config()).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Incident #3 resolved" in m.content for m in tool_messages)


def test_approving_post_to_team_channel_runs_it_and_finishes(monkeypatch):
    posted = {}
    monkeypatch.setattr(
        notify, "post_to_team_channel", lambda channel, message: posted.setdefault(channel, message)
    )

    llm = _fake_llm_returning(
        _tool_call("post_to_team_channel", {"channel": "ops-digest", "message": "all clear"}),
        AIMessage(content="Posted the update to the team channel."),
    )
    g = _build(llm)
    g.invoke(
        {"messages": [HumanMessage(content="post an update to the team")]}, config=_config()
    )
    result = g.invoke(Command(resume=True), config=_config())

    assert not g.get_state(_config()).next  # finished, not paused
    assert posted.get("ops-digest") == "all clear"
    assert result["messages"][-1].content == "Posted the update to the team channel."
