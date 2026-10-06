"""Tests for the per-tenant daily cost ceiling as runtime.py wires it
(`_check_allowance` / `_allowance_refusal`, over app/agent/budgets.py) — distinct from
app/agent/graph.py's own MAX_COST_USD_PER_TURN, which only ever sees one turn at a time.

`_check_allowance` itself is tested directly against a monkeypatched
app.agent.usage_ledger.usage_summary (no live Postgres); the rule's own cases, with explicit
arguments, are in tests/agent/test_budgets.py. The entry point (astream_events_turn) is tested
by stubbing `_allowance_refusal` to an envelope/None and asserting it never even calls
init_graph_async() when refused — proving the short-circuit happens BEFORE any real graph
work, not just that it returns the right shape.

Both `_check_allowance` and `usage_ledger.usage_summary` are `async def`
now (a real `AsyncConnectionPool`, see app/agent/sql_store.py's own
docstring).
"""

from app.agent import budgets
from app.agent import runtime as agent
from app.agent import runtime_stream as stream_module
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value


class TestCheckAllowance:
    async def test_false_when_under_the_limit(self, monkeypatch):
        from app.agent import usage_ledger

        async def fake_usage_summary(*a, **kw):
            return {"total_cost_usd": 1.0, "total_tokens": 100}

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        assert (await agent._check_allowance(TEST_CTX)).refused is False

    async def test_true_when_spend_meets_the_limit(self, monkeypatch):
        from app.agent import usage_ledger

        async def fake_usage_summary(*a, **kw):
            return {"total_cost_usd": 10.0, "total_tokens": 5000}

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        assert (await agent._check_allowance(TEST_CTX)).refused is True

    async def test_true_when_spend_exceeds_the_limit(self, monkeypatch):
        from app.agent import usage_ledger

        async def fake_usage_summary(*a, **kw):
            return {"total_cost_usd": 15.0, "total_tokens": 5000}

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        assert (await agent._check_allowance(TEST_CTX)).refused is True

    async def test_false_for_an_invalid_ctx_without_even_querying_the_ledger(self, monkeypatch):
        from app.agent import usage_ledger

        async def _fail_if_called(*a, **kw):
            raise AssertionError("usage_summary should not be queried for an invalid ctx")

        monkeypatch.setattr(usage_ledger, "usage_summary", _fail_if_called)
        assert (await agent._check_allowance(None)).refused is False
        assert (await agent._check_allowance({"tenant": "", "principal": "", "claims": {}})).refused is False

    async def test_fails_open_when_the_ledger_read_itself_raises(self, monkeypatch):
        """A usage-ledger outage must not ALSO take down every turn on top
        of whatever already took the ledger down — same degrade-don't-crash
        posture as app/retrieval/semantic_cache.py and app/agent/moderation.py."""
        from app.agent import usage_ledger

        async def _broken(*a, **kw):
            raise ConnectionError("appdata postgres unreachable")

        monkeypatch.setattr(usage_ledger, "usage_summary", _broken)
        assert (await agent._check_allowance(TEST_CTX)).refused is False

    async def test_a_warning_threshold_is_counted_once_crossed_but_the_turn_is_still_allowed(self, monkeypatch):
        from app.agent import usage_ledger

        async def fake_usage_summary(*a, **kw):
            return {"total_cost_usd": 8.5, "total_tokens": 100}

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        before = metric_value(metrics.agent_budget_threshold_total, scope="tenant", window="day", threshold="85")

        assert (await agent._check_allowance(TEST_CTX)).refused is False

        assert (
            metric_value(metrics.agent_budget_threshold_total, scope="tenant", window="day", threshold="85")
            == before + 1
        )

    async def test_queries_a_rolling_24h_window_scoped_to_this_tenant(self, monkeypatch):
        from app.agent import usage_ledger

        captured = {}

        async def fake_usage_summary(tenant, principal=None, since=None):
            captured["tenant"] = tenant
            captured["since"] = since
            return {"total_cost_usd": 0.0, "total_tokens": 0}

        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        await agent._check_allowance(TEST_CTX)

        assert captured["tenant"] == TEST_CTX["tenant"]
        assert captured["since"] is not None

    async def test_true_when_ledger_spend_plus_in_flight_reservations_meet_the_limit(
        self, monkeypatch
    ):
        """The actual race this closes: N concurrent turns for the same
        tenant would all see the SAME persisted `spent` (none of their own
        cost is recorded yet) — in_flight_reservation is what lets this
        function see the turns already running and refuse regardless."""
        from app.agent import usage_ledger

        async def fake_usage_summary(*a, **kw):
            return {"total_cost_usd": 6.0, "total_tokens": 100}

        async def fake_in_flight_reservation(tenant):
            return 4.5  # e.g. 9 concurrent turns each reserving MAX_COST_USD_PER_TURN=0.5

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        monkeypatch.setattr(usage_ledger, "in_flight_reservation", fake_in_flight_reservation)

        assert (await agent._check_allowance(TEST_CTX)).refused is True

    async def test_false_when_ledger_spend_plus_reservations_both_stay_under_the_limit(
        self, monkeypatch
    ):
        from app.agent import usage_ledger

        async def fake_usage_summary(*a, **kw):
            return {"total_cost_usd": 6.0, "total_tokens": 100}

        async def fake_in_flight_reservation(tenant):
            return 1.0

        monkeypatch.setattr(agent, "MAX_COST_USD_PER_TENANT_PER_DAY", 10.0)
        monkeypatch.setattr(usage_ledger, "usage_summary", fake_usage_summary)
        monkeypatch.setattr(usage_ledger, "in_flight_reservation", fake_in_flight_reservation)

        assert (await agent._check_allowance(TEST_CTX)).refused is False

class TestReserveAndReleaseTurnBudget:
    """runtime.py's thin wrappers around usage_ledger's reservation
    primitives — astream_events_turn calls these directly (see
    TestEntryPointsRefuseBeforeTouchingTheGraph below for the entry-point
    wiring itself)."""

    async def test_reserve_holds_the_per_turn_ceiling_and_returns_the_hold_id(self, monkeypatch):
        from app.agent import usage_ledger

        async def fake_reserve_budget(ctx, amount):
            assert amount == agent.MAX_COST_USD_PER_TURN
            return "hold-1"

        monkeypatch.setattr(usage_ledger, "reserve_budget", fake_reserve_budget)
        assert await agent._reserve_turn_budget(TEST_CTX) == "hold-1"

    async def test_reserve_returns_none_when_the_reservation_itself_fails(self, monkeypatch):
        from app.agent import usage_ledger

        async def fake_reserve_budget(ctx, amount):
            return None

        monkeypatch.setattr(usage_ledger, "reserve_budget", fake_reserve_budget)
        assert await agent._reserve_turn_budget(TEST_CTX) is None

    async def test_release_forwards_to_usage_ledger_with_the_same_hold_id(self, monkeypatch):
        from app.agent import usage_ledger

        captured = {}

        async def fake_release(ctx, hold_id):
            captured.update(ctx=ctx, hold_id=hold_id)

        monkeypatch.setattr(usage_ledger, "release_budget_reservation", fake_release)
        await agent._release_turn_budget(TEST_CTX, "hold-1")

        assert captured == {"ctx": TEST_CTX, "hold_id": "hold-1"}


class _GraphTouchedError(AssertionError):
    pass


def _forbid_graph_access(monkeypatch):
    """Any of these being called proves the over-budget check did NOT
    actually short-circuit before real graph work."""

    async def _fail_async(*a, **kw):
        raise _GraphTouchedError("graph work must not run for an over-budget tenant")

    monkeypatch.setattr(agent, "init_graph_async", _fail_async)


class TestEntryPointsRefuseBeforeTouchingTheGraph:
    async def test_astream_events_turn_short_circuits(self, monkeypatch):
        async def fake_refusal(ctx):
            return budgets.refusal_envelope(budgets.Allowance("exceeded"))

        monkeypatch.setattr(agent, "_allowance_refusal", fake_refusal)
        _forbid_graph_access(monkeypatch)

        async def _collect():
            return [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]

        events = await _collect()

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["code"] == errors.ErrorCode.TENANT_BUDGET_EXCEEDED.value

    async def test_under_budget_does_not_short_circuit(self, monkeypatch):
        """The False path must actually reach graph work — proving the
        check isn't accidentally unconditional."""

        async def fake_refusal(ctx):
            return None

        monkeypatch.setattr(agent, "_allowance_refusal", fake_refusal)
        _forbid_graph_access(monkeypatch)

        async def _collect():
            return [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]

        try:
            await _collect()
        except _GraphTouchedError:
            pass  # expected — proves init_graph_async() WAS reached this time
        else:
            raise AssertionError("expected init_graph_async() to be reached and raise")
