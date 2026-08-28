"""Tests for app/agent.py's per-tenant daily cost ceiling
(_tenant_over_daily_budget) — distinct from app/graph.py's own
MAX_COST_USD_PER_TURN, which only ever sees one turn at a time.

`_tenant_over_daily_budget` itself is tested directly against a
monkeypatched app.meter.usage_summary (no live Postgres). The three entry
points (answer/stream_turn/astream_events_turn) are tested by stubbing
`_tenant_over_daily_budget` itself to True/False and asserting they never
even call get_graph()/init_graph_async() when over budget — proving the
short-circuit happens BEFORE any real graph work, not just that it
returns the right shape.
"""
import asyncio

from app import agent, errors, metrics
from tests.conftest import TEST_CTX


class TestTenantOverDailyBudget:
    def test_false_when_under_the_limit(self, monkeypatch):
        from app import meter

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(
            meter, "usage_summary", lambda *a, **kw: {"total_cost_usd": 1.0, "total_tokens": 100}
        )
        assert agent._tenant_over_daily_budget(TEST_CTX) is False

    def test_true_when_spend_meets_the_limit(self, monkeypatch):
        from app import meter

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(
            meter, "usage_summary", lambda *a, **kw: {"total_cost_usd": 10.0, "total_tokens": 5000}
        )
        assert agent._tenant_over_daily_budget(TEST_CTX) is True

    def test_true_when_spend_exceeds_the_limit(self, monkeypatch):
        from app import meter

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(
            meter, "usage_summary", lambda *a, **kw: {"total_cost_usd": 15.0, "total_tokens": 5000}
        )
        assert agent._tenant_over_daily_budget(TEST_CTX) is True

    def test_false_for_an_invalid_ctx_without_even_querying_the_ledger(self, monkeypatch):
        from app import meter

        def _fail_if_called(*a, **kw):
            raise AssertionError("usage_summary should not be queried for an invalid ctx")

        monkeypatch.setattr(meter, "usage_summary", _fail_if_called)
        assert agent._tenant_over_daily_budget(None) is False
        assert agent._tenant_over_daily_budget({"tenant": "", "principal": "", "claims": {}}) is False

    def test_fails_open_when_the_ledger_read_itself_raises(self, monkeypatch):
        """A usage-ledger outage must not ALSO take down every turn on top
        of whatever already took the ledger down — same degrade-don't-crash
        posture as app/semantic_cache.py and app/moderation.py."""
        from app import meter

        def _broken(*a, **kw):
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(meter, "usage_summary", _broken)
        assert agent._tenant_over_daily_budget(TEST_CTX) is False

    def test_warning_metric_fires_past_80_percent_but_stays_under_the_limit(self, monkeypatch):
        from app import meter

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(
            meter, "usage_summary", lambda *a, **kw: {"total_cost_usd": 8.5, "total_tokens": 100}
        )
        before = metrics.agent_tenant_budget_warning_total._value.get()

        assert agent._tenant_over_daily_budget(TEST_CTX) is False

        assert metrics.agent_tenant_budget_warning_total._value.get() == before + 1

    def test_queries_a_rolling_24h_window_scoped_to_this_tenant(self, monkeypatch):
        from app import meter

        captured = {}

        def fake_usage_summary(tenant, principal=None, since=None):
            captured["tenant"] = tenant
            captured["since"] = since
            return {"total_cost_usd": 0.0, "total_tokens": 0}

        monkeypatch.setattr(meter, "usage_summary", fake_usage_summary)
        agent._tenant_over_daily_budget(TEST_CTX)

        assert captured["tenant"] == TEST_CTX["tenant"]
        assert captured["since"] is not None


class _GraphTouchedError(AssertionError):
    pass


def _forbid_graph_access(monkeypatch):
    """Any of these being called proves the over-budget check did NOT
    actually short-circuit before real graph work."""

    def _fail(*a, **kw):
        raise _GraphTouchedError("graph work must not run for an over-budget tenant")

    monkeypatch.setattr(agent, "get_graph", _fail)

    async def _fail_async(*a, **kw):
        raise _GraphTouchedError("graph work must not run for an over-budget tenant")

    monkeypatch.setattr(agent, "init_graph_async", _fail_async)


class TestEntryPointsRefuseBeforeTouchingTheGraph:
    def test_answer_short_circuits(self, monkeypatch):
        monkeypatch.setattr(agent, "_tenant_over_daily_budget", lambda ctx: True)
        _forbid_graph_access(monkeypatch)

        text, citations, error, ungrounded = agent.answer("hi", "t1", TEST_CTX)

        assert error is not None
        assert error.code == errors.ErrorCode.TENANT_BUDGET_EXCEEDED
        assert citations == []
        assert ungrounded == 0

    def test_stream_turn_short_circuits(self, monkeypatch):
        monkeypatch.setattr(agent, "_tenant_over_daily_budget", lambda ctx: True)
        _forbid_graph_access(monkeypatch)

        chunks = list(agent.stream_turn("hi", "t1", TEST_CTX))

        assert any("budget" in c.lower() for c in chunks)

    def test_astream_events_turn_short_circuits(self, monkeypatch):
        monkeypatch.setattr(agent, "_tenant_over_daily_budget", lambda ctx: True)
        _forbid_graph_access(monkeypatch)

        async def _collect():
            return [event async for event in agent.astream_events_turn("hi", "t1", TEST_CTX)]

        events = asyncio.run(_collect())

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == errors.ErrorCode.TENANT_BUDGET_EXCEEDED.value

    def test_under_budget_does_not_short_circuit(self, monkeypatch):
        """The False path must actually reach graph work — proving the
        check isn't accidentally unconditional."""
        monkeypatch.setattr(agent, "_tenant_over_daily_budget", lambda ctx: False)
        _forbid_graph_access(monkeypatch)

        try:
            agent.answer("hi", "t1", TEST_CTX)
        except _GraphTouchedError:
            pass  # expected — proves get_graph() WAS reached this time
        else:
            raise AssertionError("expected get_graph() to be reached and raise")
