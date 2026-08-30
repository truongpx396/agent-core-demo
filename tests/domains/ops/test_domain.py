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
    }


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
