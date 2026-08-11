"""Tests for Prometheus metrics wiring: the tool-call callback handler and
the direct increments inside graph.py's nodes for events a generic
callback can't reliably distinguish (see app/metrics.py's module docstring
for why the split exists).

These read Counter internals directly (`._value.get()` /
`.labels(...)._value.get()`) since prometheus_client doesn't expose a
public getter — a common pattern for testing prometheus_client
instrumentation. All assertions use deltas (before/after), not absolute
values, since these are global, process-wide counters shared across the
whole test session/process.
"""
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app import graph, metrics
from app.graph import build_graph, human_approval, retry_output, too_many_tool_calls


def _count(counter, **labels):
    target = counter.labels(**labels) if labels else counter
    return target._value.get()


def _config():
    return {
        "configurable": {"thread_id": str(uuid.uuid4())},
        "callbacks": [metrics.MetricsCallbackHandler()],
    }


def _tool_call_message(name, args, call_id="call_1"):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


class TestNodeLevelMetrics:
    def test_retry_output_increments_retry_counter(self):
        before = _count(metrics.agent_retry_total)
        retry_output({"messages": []})
        assert _count(metrics.agent_retry_total) == before + 1

    def test_human_approval_approved_increments_with_correct_label(self, monkeypatch):
        monkeypatch.setattr(graph, "interrupt", lambda payload: True)
        before = _count(metrics.agent_human_approval_total, decision="approved")
        ai = _tool_call_message("calculator", {"expression": "1+1"})
        human_approval({"messages": [ai]})
        assert (
            _count(metrics.agent_human_approval_total, decision="approved")
            == before + 1
        )

    def test_human_approval_rejected_increments_with_correct_label(self, monkeypatch):
        monkeypatch.setattr(graph, "interrupt", lambda payload: False)
        before = _count(metrics.agent_human_approval_total, decision="rejected")
        ai = _tool_call_message("calculator", {"expression": "1+1"})
        human_approval({"messages": [ai]})
        assert (
            _count(metrics.agent_human_approval_total, decision="rejected")
            == before + 1
        )

    def test_too_many_tool_calls_increments_budget_counter(self):
        before = _count(metrics.agent_tool_budget_exceeded_total)
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": "calculator", "args": {"expression": "1+1"}, "id": f"c{i}"}
                for i in range(6)
            ],
        )
        too_many_tool_calls({"messages": [ai]})
        assert _count(metrics.agent_tool_budget_exceeded_total) == before + 1


class TestToolCallbackMetrics:
    """MetricsCallbackHandler.on_tool_start / on_tool_error fire from
    inside ToolNode's real tool execution, so these run through the whole
    compiled graph rather than calling a node function directly."""

    def test_tool_call_via_graph_increments_tool_calls_and_label(self):
        before = _count(metrics.agent_tool_calls_total, tool="calculator")
        llm = GenericFakeChatModel(
            messages=iter(
                [
                    _tool_call_message("calculator", {"expression": "2+2"}),
                    AIMessage(content="2 plus 2 equals 4, a proper final answer."),
                ]
            )
        )
        g = build_graph(llm=llm)
        g.invoke(
            {"messages": [HumanMessage(content="what is 2+2?")]}, config=_config()
        )
        assert _count(metrics.agent_tool_calls_total, tool="calculator") == before + 1

    def test_tool_error_via_graph_increments_error_counter(self):
        before = _count(metrics.agent_tool_errors_total)
        llm = GenericFakeChatModel(
            messages=iter(
                [
                    # Blank expression fails CalculatorArgs._not_blank validation.
                    _tool_call_message("calculator", {"expression": ""}),
                    AIMessage(content="Sorry, I could not run that calculation."),
                ]
            )
        )
        g = build_graph(llm=llm)
        g.invoke(
            {"messages": [HumanMessage(content="what is nothing?")]}, config=_config()
        )
        assert _count(metrics.agent_tool_errors_total) == before + 1


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_prometheus_text(self):
        from fastapi.testclient import TestClient

        from app.api import app

        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "agent_requests_total" in resp.text
