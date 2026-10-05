"""app/agent/budgets.py — the tenant allowance rule, with its inputs passed explicitly.

tests/agent/test_tenant_budget.py covers the same rule as runtime.py wires it (module globals
re-pointed per test) and the entry points' short-circuit. This file calls the rule directly, so
each case names exactly the limit, the spend and the failure policy it is about — including the
fail-closed option (`BUDGET_CHECK_FAILURE_POLICY`) that the old bool-returning check could not
express: it had no way to say "refused, but not because you are over budget".
"""
import logging

import pytest

from app.agent import budgets, usage_ledger
from app.agent import runtime as runtime_module
from app.agent import runtime_stream as stream_module
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value

LIMIT = 10.0


@pytest.fixture
def ledger(monkeypatch):
    """A ledger whose 24h spend and in-flight holds the test sets."""
    state = {"spent": 0.0, "reserved": 0.0, "read_error": None, "since": None, "tenant": None}

    async def usage_summary(tenant, principal=None, since=None):
        if state["read_error"]:
            raise state["read_error"]
        state.update(tenant=tenant, since=since)
        return {"total_cost_usd": state["spent"], "total_tokens": 0}

    async def in_flight_reservation(tenant):
        return state["reserved"]

    monkeypatch.setattr(usage_ledger, "usage_summary", usage_summary)
    monkeypatch.setattr(usage_ledger, "in_flight_reservation", in_flight_reservation)
    return state


async def _check(fail_policy="open", ctx=TEST_CTX, limit=LIMIT):
    return await budgets.check_tenant_daily(ctx, limit_usd=limit, warning_fraction=0.8, fail_policy=fail_policy)


class TestCheckTenantDaily:
    async def test_under_the_limit_is_ok_and_reports_the_figures(self, ledger):
        ledger.update(spent=1.0, reserved=0.5)

        allowance = await _check()

        assert allowance == budgets.Allowance("ok", spent_usd=1.0, reserved_usd=0.5, limit_usd=LIMIT)
        assert allowance.refused is False and allowance.degraded is False

    @pytest.mark.parametrize("spent", [10.0, 15.0])
    async def test_at_or_past_the_limit_is_exceeded(self, ledger, spent):
        ledger["spent"] = spent

        allowance = await _check()

        assert allowance.status == "exceeded" and allowance.refused is True

    async def test_spend_plus_in_flight_holds_count_together(self, ledger):
        """The race the holds exist for: N concurrent turns all read the same persisted spend."""
        ledger.update(spent=6.0, reserved=4.5)

        assert (await _check()).status == "exceeded"

    async def test_an_exceeded_turn_is_counted_and_logged_with_its_tenant_and_principal(self, ledger, caplog):
        """The counter has no tenant label by design (cardinality), so the log line is the only
        place an operator can learn WHICH tenant is being refused (spec 008 A4)."""
        ledger["spent"] = 12.0
        before = metric_value(metrics.agent_tenant_budget_exceeded_total)

        with caplog.at_level(logging.WARNING, logger=budgets.logger.name):
            await _check()

        assert metric_value(metrics.agent_tenant_budget_exceeded_total) == before + 1
        record = next(r for r in caplog.records if r.getMessage() == "tenant_budget_exceeded")
        assert (record.tenant, record.principal) == (TEST_CTX["tenant"], TEST_CTX["principal"])
        assert (record.spent_usd, record.limit_usd) == (12.0, LIMIT)

    async def test_past_the_warning_fraction_proceeds_but_is_counted(self, ledger):
        ledger["spent"] = 8.5
        before = metric_value(metrics.agent_tenant_budget_warning_total)

        allowance = await _check()

        assert allowance.refused is False
        assert metric_value(metrics.agent_tenant_budget_warning_total) == before + 1

    async def test_under_the_warning_fraction_is_silent(self, ledger):
        ledger["spent"] = 7.9
        before = metric_value(metrics.agent_tenant_budget_warning_total)

        await _check()

        assert metric_value(metrics.agent_tenant_budget_warning_total) == before

    async def test_reads_a_rolling_24_hour_window_for_this_tenant_only(self, ledger):
        await _check()

        assert ledger["tenant"] == TEST_CTX["tenant"]
        assert ledger["since"] is not None

    @pytest.mark.parametrize("ctx", [None, {"tenant": "", "principal": "", "claims": {}}])
    async def test_an_unattributable_ctx_is_ok_without_reading_the_ledger(self, monkeypatch, ctx):
        async def fail(*args, **kwargs):
            raise AssertionError("nothing to meter for an invalid ctx")

        monkeypatch.setattr(usage_ledger, "usage_summary", fail)

        assert (await _check(ctx=ctx)).status == "ok"


class TestWhenTheLedgerCannotBeRead:
    async def test_the_open_policy_serves_the_turn_but_says_it_was_not_verified(self, ledger):
        ledger["read_error"] = ConnectionError("appdata postgres unreachable")
        before = metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read")

        allowance = await _check("open")

        assert allowance.status == "ok" and allowance.refused is False
        assert allowance.degraded is True
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read") == before + 1

    async def test_the_closed_policy_refuses_the_turn_as_unavailable_not_as_exceeded(self, ledger):
        ledger["read_error"] = ConnectionError("appdata postgres unreachable")
        exceeded_before = metric_value(metrics.agent_tenant_budget_exceeded_total)
        degraded_before = metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read")

        allowance = await _check("closed")

        assert allowance.status == "unavailable" and allowance.refused is True
        assert allowance.degraded is False
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read") == degraded_before + 1
        # an unverifiable tenant is not an over-budget one: the budget alerts must not fire on it
        assert metric_value(metrics.agent_tenant_budget_exceeded_total) == exceeded_before


class TestRefusalEnvelope:
    def test_an_exceeded_allowance_is_the_tenant_budget_error(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("exceeded"))

        assert envelope.code == errors.ErrorCode.TENANT_BUDGET_EXCEEDED
        assert "daily usage budget" in envelope.message

    def test_an_unavailable_check_is_its_own_error_that_blames_no_budget(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("unavailable"))

        assert envelope.code == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE
        assert "budget" not in envelope.message.lower()


class TestAsRuntimeWiresIt:
    async def test_the_failure_policy_is_read_from_the_runtime_module_at_call_time(self, ledger, monkeypatch):
        ledger["read_error"] = ConnectionError("down")

        monkeypatch.setattr(runtime_module, "BUDGET_CHECK_FAILURE_POLICY", "open")
        assert await runtime_module._allowance_refusal(TEST_CTX) is None

        monkeypatch.setattr(runtime_module, "BUDGET_CHECK_FAILURE_POLICY", "closed")
        refusal = await runtime_module._allowance_refusal(TEST_CTX)
        assert refusal is not None and refusal.code == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE

    async def test_a_turn_is_refused_before_any_graph_work_when_the_check_cannot_be_made(self, ledger, monkeypatch):
        ledger["read_error"] = ConnectionError("down")
        monkeypatch.setattr(runtime_module, "BUDGET_CHECK_FAILURE_POLICY", "closed")

        async def graph_must_not_be_touched(*args, **kwargs):
            raise AssertionError("a refused turn must never reach the graph")

        monkeypatch.setattr(runtime_module, "init_graph_async", graph_must_not_be_touched)

        events = [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]

        assert len(events) == 1
        assert events[0]["code"] == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE.value

    async def test_a_resume_is_refused_the_same_way(self, ledger, monkeypatch):
        ledger["read_error"] = ConnectionError("down")
        monkeypatch.setattr(runtime_module, "BUDGET_CHECK_FAILURE_POLICY", "closed")

        events = [event async for event in stream_module.astream_events_resume("t1", True, TEST_CTX)]

        assert events[0]["code"] == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE.value
