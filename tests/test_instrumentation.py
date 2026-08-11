"""Tests for `_instrumented`, the structured lifecycle-logging wrapper
applied to every node at graph-registration time (see build_graph and
GRAPH_PATTERNS.md pattern 14).

These test the decorator in isolation with a throwaway node function, not
through a compiled graph — the compiled-graph HITL pause/resume behavior
this wrapper must not break is already covered end-to-end by
TestHumanApprovalPath in test_graph_integration.py.
"""
import logging

import pytest
from langgraph.errors import GraphInterrupt

from app.graph import _instrumented


def test_wraps_node_and_returns_its_result_unchanged():
    @_instrumented("dummy")
    def node(state):
        return {"ok": True}

    assert node({"run_id": "abc123"}) == {"ok": True}


def test_logs_started_and_completed_on_success(caplog):
    @_instrumented("dummy")
    def node(state):
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="app.graph"):
        node({"run_id": "abc123"})

    messages = [r.message for r in caplog.records]
    assert "node_started" in messages
    assert "node_completed" in messages
    assert "node_failed" not in messages

    completed = next(r for r in caplog.records if r.message == "node_completed")
    assert completed.node == "dummy"
    assert completed.run_id == "abc123"
    assert isinstance(completed.duration_ms, int)


def test_missing_run_id_logs_as_placeholder_not_a_crash():
    @_instrumented("dummy")
    def node(state):
        return {}

    # No run_id key at all — e.g. a node called directly in a unit test
    # with a hand-built state dict, outside validate_input's reset.
    assert node({}) == {}


def test_logs_failed_and_reraises_on_exception(caplog):
    @_instrumented("dummy")
    def node(state):
        raise ValueError("boom")

    with caplog.at_level(logging.WARNING, logger="app.graph"):
        with pytest.raises(ValueError, match="boom"):
            node({"run_id": "abc123"})

    failed = next(r for r in caplog.records if r.message == "node_failed")
    assert failed.node == "dummy"
    assert failed.run_id == "abc123"
    assert failed.error_class == "ValueError"


def test_graph_interrupt_is_logged_as_paused_not_failed_and_still_propagates(caplog):
    """human_approval's interrupt() raises GraphInterrupt to pause the
    run — normal control flow, not a node failure. It must be logged
    distinctly and re-raised untouched, or LangGraph can't actually pause
    the graph (see _instrumented's docstring)."""

    @_instrumented("dummy")
    def node(state):
        raise GraphInterrupt()

    with caplog.at_level(logging.INFO, logger="app.graph"):
        with pytest.raises(GraphInterrupt):
            node({"run_id": "abc123"})

    messages = [r.message for r in caplog.records]
    assert "node_paused" in messages
    assert "node_failed" not in messages
