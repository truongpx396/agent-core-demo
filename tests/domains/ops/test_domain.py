"""Proves the ops-bot domain (app/domains/ops/) on the same seam as
tests/domains/support/test_domain.py / tests/domains/sales/test_domain.py
— this domain's ToolNode only knows its own tools, and its `outward`
tool (post_to_team_channel — this repo's first real use of that
capability, see GRAPH_PATTERNS.md pattern 47) is gated exactly like a
mutating one.

The compiled graph's `agent`/`retrieve_context`/etc. nodes are `async def`
now (real LLM/Redis/Qdrant I/O — see app/agent/graph.py), so every
`g.invoke`/`g.get_state` below runs as `asyncio.run(g.ainvoke(...))`/
`asyncio.run(g.aget_state(...))` instead — LangGraph's sync Pregel loop
can't run an async-only node at all. Each call gets its own `asyncio.run`
rather than one shared event loop across a test, which is fine here since
this domain's graphs use the default in-memory MemorySaver (no event-loop-
bound state — contrast with `AsyncPostgresSaver`'s per-instance
`asyncio.Lock`, see app/agent/runtime.py's module docstring).
"""
import asyncio

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
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


_CORE_OPS_TOOLS = {
    "fetch_metrics_summary",
    "post_to_team_channel",
    "ask_clarification",
    "skill_search",
    "use_skill",
    "log_incident",
    "list_recent_incidents",
    "resolve_incident",
    "check_vendor_status_page",
    "run_subagent",
}

_SANDBOX_TOOLS = {"run_command_in_sandbox", "run_python_in_sandbox", "read_sandbox_file", "write_sandbox_file"}


def test_tool_node_knows_at_least_the_core_ops_domains_tools():
    """Subset, not exact-set, check — leaves room for the manifest to grow
    without this test becoming a maintenance chore for every addition."""
    g = _build()
    assert _CORE_OPS_TOOLS <= set(g.nodes["tools"].bound.tools_by_name)


def test_sandbox_tools_are_always_present_as_a_fixed_set():
    """Unlike the old raw ~19-tool OpenSandbox catalog this domain used to
    merge in directly, the exposed sandbox surface is exactly these four
    names, always — app/domains/ops/tools.py builds all four (run_command_in_sandbox,
    run_python_in_sandbox, read_sandbox_file, write_sandbox_file) from the
    SAME load_raw_sandbox_tools() call and adds them unconditionally, no
    longer gated on opensandbox-mcp's reachability at import time (that
    gate caused a real, live bug — a process that booted before
    opensandbox-server was ready stayed permanently blind to all of them;
    see sandbox_session.load_raw_sandbox_tools's own docstring).
    Reachability is now a per-call concern (each impl calls
    load_raw_sandbox_tools() fresh and raises a plain error if it's still
    unreachable), not a presence concern — covered by
    tests/domains/test_sandbox_session.py (hermetic) and
    tests/live/test_sandbox_session_live.py (real bridge)."""
    g = _build()
    assert _SANDBOX_TOOLS <= set(g.nodes["tools"].bound.tools_by_name)


def test_every_sandbox_tool_present_is_declared_outward():
    g = _build()
    present = _SANDBOX_TOOLS & set(g.nodes["tools"].bound.tools_by_name)
    capabilities = OPS_DOMAIN_PLUGIN.tool_capabilities()
    for name in present:
        assert capabilities.get(name) == "outward", name


class TestDomainScopedSubagent:
    """Same proof as tests/domains/support/test_domain.py's own
    TestDomainScopedSubagent: run_subagent is this domain's OWN
    closure-built tool, and its menu offers only the bundled subagent(s)
    declared `domains: [ops]` (subagents/metrics-researcher/AGENT.md)."""

    def test_is_not_the_ecorp_level_run_subagent_object(self):
        from app.agent.subagent_tools import run_subagent as ecorp_run_subagent

        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        assert domain_run_subagent is not ecorp_run_subagent

    def test_menu_offers_only_the_ops_domains_own_subagents(self):
        g = _build()
        domain_run_subagent = g.nodes["tools"].bound.tools_by_name["run_subagent"]
        schema = domain_run_subagent.args_schema.model_json_schema()
        enum_def = next(iter(schema["$defs"].values()))
        assert set(enum_def["enum"]) == {"metrics-researcher", "vendor-history-researcher"}

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
                {"subagent_name": "metrics-researcher", "task": "has latency spiked before?"},
            ),
            AIMessage(content="Here's what the subagent found out for you."),
        )
        g = _build(llm)
        asyncio.run(g.ainvoke(
            {"messages": [HumanMessage(content="has this happened before?")]}, config=_config()
        ))
        assert not asyncio.run(g.aget_state(_config())).next  # never paused


def test_fetch_metrics_summary_is_read_only_and_never_pauses(monkeypatch):
    from app.domains.ops import metrics_client

    monkeypatch.setattr(metrics_client, "fetch_readings", lambda: {})
    llm = _fake_llm_returning(
        _tool_call("fetch_metrics_summary", {}),
        AIMessage(content="Everything looks normal."),
    )
    g = _build(llm)
    result = asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="is everything ok?")]}, config=_config()
    ))
    assert not asyncio.run(g.aget_state(_config())).next  # never paused
    assert result["messages"][-1].content == "Everything looks normal."


def test_post_to_team_channel_pauses_for_approval_as_an_outward_tool():
    llm = _fake_llm_returning(
        _tool_call("post_to_team_channel", {"channel": "ops-digest", "message": "all clear"})
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="post an update to the team")]}, config=_config()
    ))
    assert asyncio.run(g.aget_state(_config())).next  # paused, not finished


def test_list_recent_incidents_is_read_only_and_never_pauses(monkeypatch):
    from app.domains.ops import store

    monkeypatch.setattr(store, "list_recent_incidents", lambda limit=10, status=None: [])
    llm = _fake_llm_returning(
        _tool_call("list_recent_incidents", {}),
        AIMessage(content="No incidents on record."),
    )
    g = _build(llm)
    result = asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="has this happened before?")]}, config=_config()
    ))
    assert not asyncio.run(g.aget_state(_config())).next  # never paused
    assert result["messages"][-1].content == "No incidents on record."


def test_log_incident_pauses_for_approval_and_runs_once_approved(monkeypatch):
    from app.domains.ops import store

    monkeypatch.setattr(store, "log_incident", lambda opened_by, summary, detail: 3)

    llm = _fake_llm_returning(
        _tool_call("log_incident", {"summary": "latency spike", "detail": "p95 at 45s"}),
        AIMessage(content="Logged incident #3."),
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="latency looks bad, log it")]}, config=_config()
    ))
    assert asyncio.run(g.aget_state(_config())).next  # paused, not finished

    result = asyncio.run(g.ainvoke(Command(resume=True), config=_config()))
    assert not asyncio.run(g.aget_state(_config())).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Incident #3 logged" in m.content for m in tool_messages)


def test_run_command_in_sandbox_pauses_for_approval_and_runs_once_approved(monkeypatch):
    """The sandbox trio is always present now (see
    test_sandbox_tools_are_always_present_as_a_fixed_set above), but its
    impl still calls load_raw_sandbox_tools() fresh on every real call —
    stub that out too, not just run_command_in_sandbox_impl, so this test
    doesn't depend on opensandbox-mcp actually being reachable."""
    from app.domains.ops import tools as ops_tools

    monkeypatch.setattr(ops_tools.sandbox_session, "load_raw_sandbox_tools", lambda: {"command_run": object()})
    monkeypatch.setattr(
        ops_tools.sandbox_session,
        "run_command_in_sandbox_impl",
        lambda command, thread_id, raw: "exit code: 0\nstdout:\n42.75\n",
    )

    llm = _fake_llm_returning(
        _tool_call("run_command_in_sandbox", {"command": "python3 -c 'print(42.75)'"}),
        AIMessage(content="The 95th percentile is 42.75."),
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="compute the 95th percentile of these numbers")]},
        config=_config(),
    ))
    assert asyncio.run(g.aget_state(_config())).next  # paused, not finished

    result = asyncio.run(g.ainvoke(Command(resume=True), config=_config()))
    assert not asyncio.run(g.aget_state(_config())).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("42.75" in m.content for m in tool_messages)


def test_run_python_in_sandbox_pauses_for_approval_and_runs_once_approved(monkeypatch):
    """Same shape as run_command_in_sandbox's own version of this test —
    run_python_in_sandbox exists specifically so the model can pass a
    real multi-line script (quotes, apostrophes, f-strings all fine) as
    a plain parameter instead of fighting shell quoting via
    `python -c '...'` (a real, repeatedly-observed failure mode)."""
    from app.domains.ops import tools as ops_tools

    monkeypatch.setattr(ops_tools.sandbox_session, "load_raw_sandbox_tools", lambda: {"command_run": object()})
    monkeypatch.setattr(
        ops_tools.sandbox_session,
        "run_python_in_sandbox_impl",
        lambda script, thread_id, raw: "exit code: 0\nstdout:\n42.75\n",
    )

    llm = _fake_llm_returning(
        _tool_call("run_python_in_sandbox", {"script": "print(42.75)"}),
        AIMessage(content="The 95th percentile is 42.75."),
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="compute the 95th percentile of these numbers")]},
        config=_config(),
    ))
    assert asyncio.run(g.aget_state(_config())).next  # paused, not finished

    result = asyncio.run(g.ainvoke(Command(resume=True), config=_config()))
    assert not asyncio.run(g.aget_state(_config())).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("42.75" in m.content for m in tool_messages)


def test_resolve_incident_pauses_for_approval_and_runs_once_approved(monkeypatch):
    from app.domains.ops import store

    monkeypatch.setattr(store, "resolve_incident", lambda incident_id, resolution: True)

    llm = _fake_llm_returning(
        _tool_call("resolve_incident", {"incident_id": 3, "resolution": "restarted the worker"}),
        AIMessage(content="Resolved incident #3."),
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="incident 3 is fixed now")]}, config=_config()
    ))
    assert asyncio.run(g.aget_state(_config())).next  # paused, not finished

    result = asyncio.run(g.ainvoke(Command(resume=True), config=_config()))
    assert not asyncio.run(g.aget_state(_config())).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("Incident #3 resolved" in m.content for m in tool_messages)


def test_check_vendor_status_page_pauses_for_approval_as_an_outward_tool():
    llm = _fake_llm_returning(
        _tool_call("check_vendor_status_page", {"url": "https://status.example.com"})
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="is our payment processor having an outage?")]},
        config=_config(),
    ))
    assert asyncio.run(g.aget_state(_config())).next  # paused, not finished


def test_approving_check_vendor_status_page_runs_it_and_finishes(monkeypatch):
    from app.domains.ops import tools as ops_tools

    monkeypatch.setattr(
        ops_tools, "render_url_to_markdown", lambda url: "All systems operational."
    )

    llm = _fake_llm_returning(
        _tool_call("check_vendor_status_page", {"url": "https://status.example.com"}),
        AIMessage(content="Their status page shows no ongoing incident."),
    )
    g = _build(llm)
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="is our payment processor having an outage?")]},
        config=_config(),
    ))
    result = asyncio.run(g.ainvoke(Command(resume=True), config=_config()))

    assert not asyncio.run(g.aget_state(_config())).next  # finished, not paused
    tool_messages = [m for m in result["messages"] if m.type == "tool"]
    assert any("All systems operational." in m.content for m in tool_messages)


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
    asyncio.run(g.ainvoke(
        {"messages": [HumanMessage(content="post an update to the team")]}, config=_config()
    ))
    result = asyncio.run(g.ainvoke(Command(resume=True), config=_config()))

    assert not asyncio.run(g.aget_state(_config())).next  # finished, not paused
    assert posted.get("ops-digest") == "all clear"
    assert result["messages"][-1].content == "Posted the update to the team channel."
