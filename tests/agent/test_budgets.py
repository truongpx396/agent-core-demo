"""app/agent/budgets.py — the spend allowance, with its inputs passed explicitly.

tests/agent/test_tenant_budget.py covers the same rule as runtime.py wires it (module globals
re-pointed per test) and the entry points' short-circuit. This file calls the rule directly, so
each case names exactly the limits, the spend, the clock and the failure policy it is about.

Time is injected (`now=`), because two of the four limits are calendar-month windows: their
boundaries — the 1st, and December rolling into January — are exactly where a date calculation
goes wrong, and a test that depends on today's date can only fail on the day it matters.
"""
import logging
from datetime import UTC, datetime

import pytest

from app.agent import budgets, spend, usage_ledger
from app.agent import runtime as runtime_module
from app.agent import runtime_stream as stream_module
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
DAY_START = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
MONTH_START = datetime(2026, 10, 1, tzinfo=UTC)

TENANT_DAY = budgets.BudgetLimit("tenant", "day", 10.0)
TENANT_MONTH = budgets.BudgetLimit("tenant", "month", 100.0)
PERSON_DAY = budgets.BudgetLimit("principal", "day", 5.0)
PERSON_MONTH = budgets.BudgetLimit("principal", "month", 50.0)
ALL_LIMITS = [TENANT_DAY, TENANT_MONTH, PERSON_DAY, PERSON_MONTH]


@pytest.fixture
def ledger(monkeypatch):
    """A ledger whose spend the test sets per (scope, window), recording every read."""
    state = {
        "spend": {},
        "reserved": 0.0,
        "read_error": None,
        "reads": [],
    }

    async def usage_summary(tenant, principal=None, since=None):
        if state["read_error"]:
            raise state["read_error"]
        scope = "tenant" if principal is None else "principal"
        window = "month" if since == MONTH_START else "day"
        state["reads"].append({"tenant": tenant, "principal": principal, "since": since})
        return {"total_cost_usd": state["spend"].get((scope, window), 0.0), "total_tokens": 0}

    async def in_flight_reservation(tenant):
        return state["reserved"]

    monkeypatch.setattr(spend, "usage_summary", usage_summary)
    monkeypatch.setattr(usage_ledger, "in_flight_reservation", in_flight_reservation)
    return state


async def _check(limits=(TENANT_DAY,), fail_policy="open", ctx=TEST_CTX):
    return await budgets.check_allowance(ctx, limits=limits, fail_policy=fail_policy, now=NOW)


def _exceeded(scope="tenant", window="day"):
    return metric_value(metrics.agent_budget_exceeded_total, scope=scope, window=window)


def _threshold(threshold, scope="tenant", window="day"):
    return metric_value(metrics.agent_budget_threshold_total, scope=scope, window=window, threshold=threshold)


class TestWindows:
    def test_the_day_window_is_the_trailing_24_hours_not_a_calendar_day(self):
        assert budgets.window_start("day", NOW) == DAY_START

    def test_the_month_window_starts_at_midnight_utc_on_the_1st(self):
        assert budgets.window_start("month", NOW) == MONTH_START

    def test_the_first_instant_of_a_month_is_its_own_window_start(self):
        first = datetime(2026, 11, 1, tzinfo=UTC)

        assert budgets.window_start("month", first) == first

    def test_a_month_resets_on_the_1st_of_the_next_one(self):
        assert budgets.window_resets_at("month", NOW) == datetime(2026, 11, 1, tzinfo=UTC)

    def test_december_rolls_over_into_january_of_the_next_year(self):
        assert budgets.window_resets_at("month", datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
            2027, 1, 1, tzinfo=UTC
        )

    def test_the_rolling_day_has_no_single_reset_instant(self):
        assert budgets.window_resets_at("day", NOW) is None


class TestConfiguredLimits:
    def test_the_tenant_daily_limit_is_always_present_even_at_zero(self):
        """A zero there has always meant "refuse everything"; that must not change."""
        assert budgets.configured_limits(tenant_day=0.0) == [budgets.BudgetLimit("tenant", "day", 0.0)]

    def test_the_other_three_are_present_only_when_above_zero(self):
        assert budgets.configured_limits(tenant_day=10.0, principal_day=5.0) == [
            TENANT_DAY,
            PERSON_DAY,
        ]

    def test_tenant_limits_come_before_personal_ones(self):
        limits = budgets.configured_limits(tenant_day=10.0, tenant_month=100.0, principal_day=5.0, principal_month=50.0)

        assert limits == ALL_LIMITS


class TestCheckAllowance:
    async def test_under_every_limit_is_ok(self, ledger):
        ledger["spend"] = {("tenant", "day"): 1.0, ("principal", "day"): 1.0}

        allowance = await _check([TENANT_DAY, PERSON_DAY])

        assert allowance.status == "ok" and allowance.refused is False and allowance.degraded is False

    @pytest.mark.parametrize("spent", [10.0, 15.0])
    async def test_at_or_past_a_limit_is_exceeded(self, ledger, spent):
        ledger["spend"] = {("tenant", "day"): spent}

        allowance = await _check()

        assert allowance.status == "exceeded" and allowance.refused is True
        assert (allowance.scope, allowance.window, allowance.limit_usd) == ("tenant", "day", 10.0)

    async def test_a_personal_limit_refuses_while_the_tenant_is_fine(self, ledger):
        ledger["spend"] = {("tenant", "day"): 1.0, ("principal", "day"): 5.0}

        allowance = await _check([TENANT_DAY, PERSON_DAY])

        assert (allowance.status, allowance.scope, allowance.window) == ("exceeded", "principal", "day")

    async def test_a_monthly_limit_refuses_while_the_daily_one_is_fine(self, ledger):
        ledger["spend"] = {("tenant", "day"): 1.0, ("tenant", "month"): 100.0}

        allowance = await _check([TENANT_DAY, TENANT_MONTH])

        assert (allowance.scope, allowance.window) == ("tenant", "month")
        assert allowance.resets_at == datetime(2026, 11, 1, tzinfo=UTC)

    async def test_when_two_limits_are_exceeded_the_tenant_one_wins_and_only_it_is_counted(self, ledger):
        ledger["spend"] = {("tenant", "day"): 12.0, ("principal", "day"): 6.0}
        tenant_before, person_before = _exceeded("tenant", "day"), _exceeded("principal", "day")

        allowance = await _check([TENANT_DAY, PERSON_DAY])

        assert allowance.scope == "tenant"
        assert _exceeded("tenant", "day") == tenant_before + 1
        assert _exceeded("principal", "day") == person_before

    async def test_each_limit_reads_its_own_window_and_only_a_personal_one_names_the_person(self, ledger):
        await _check(ALL_LIMITS)

        reads = ledger["reads"]
        assert [(r["principal"], r["since"]) for r in reads] == [
            (None, DAY_START),
            (None, MONTH_START),
            (TEST_CTX["principal"], DAY_START),
            (TEST_CTX["principal"], MONTH_START),
        ]
        assert {r["tenant"] for r in reads} == {TEST_CTX["tenant"]}  # no limit ever crosses tenants

    async def test_only_enabled_limits_are_read(self, ledger):
        """A deployment with just the daily tenant limit pays for exactly one ledger read."""
        await _check([TENANT_DAY])

        assert len(ledger["reads"]) == 1

    async def test_in_flight_holds_count_against_tenant_limits_together_with_spend(self, ledger):
        """The race the holds exist for: N concurrent turns all read the same persisted spend."""
        ledger["spend"] = {("tenant", "day"): 6.0}
        ledger["reserved"] = 4.5

        assert (await _check()).status == "exceeded"

    async def test_in_flight_holds_do_not_count_against_a_personal_limit(self, ledger):
        """Holds are per tenant (see the module docstring): a person's limit is a soft guard rail."""
        ledger["spend"] = {("principal", "day"): 1.0}
        ledger["reserved"] = 99.0

        assert (await _check([PERSON_DAY])).status == "ok"

    async def test_a_refusal_is_counted_under_its_scope_and_window_and_logged_with_who(self, ledger, caplog):
        """The counter has no tenant label by design (cardinality), so the log line is the only
        place an operator can learn WHICH tenant or person is being refused (spec 008 A4)."""
        ledger["spend"] = {("principal", "month"): 60.0}
        before = _exceeded("principal", "month")

        with caplog.at_level(logging.WARNING, logger=budgets.logger.name):
            await _check([PERSON_MONTH])

        assert _exceeded("principal", "month") == before + 1
        record = next(r for r in caplog.records if r.getMessage() == "budget_exceeded")
        assert (record.tenant, record.principal) == (TEST_CTX["tenant"], TEST_CTX["principal"])
        assert (record.scope, record.window, record.spent_usd, record.limit_usd) == ("principal", "month", 60.0, 50.0)

    @pytest.mark.parametrize("ctx", [None, {"tenant": "", "principal": "", "claims": {}}])
    async def test_an_unattributable_ctx_is_ok_without_reading_the_ledger(self, monkeypatch, ctx):
        async def fail(*args, **kwargs):
            raise AssertionError("nothing to meter for an invalid ctx")

        monkeypatch.setattr(spend, "usage_summary", fail)

        assert (await _check(ctx=ctx)).status == "ok"


class TestWarningThresholds:
    @pytest.mark.parametrize(
        ("spent", "expected"),
        [(6.9, None), (7.0, "70"), (8.4, "70"), (8.5, "85"), (9.4, "85"), (9.5, "95"), (9.99, "95")],
    )
    async def test_only_the_highest_crossed_threshold_is_counted(self, ledger, spent, expected):
        ledger["spend"] = {("tenant", "day"): spent}
        before = {t: _threshold(t) for t in ("70", "85", "95")}

        allowance = await _check()

        assert allowance.refused is False
        after = {t: _threshold(t) for t in ("70", "85", "95")}
        assert {t: after[t] - before[t] for t in before} == {t: int(t == expected) for t in before}

    async def test_a_threshold_is_counted_under_its_scope_and_window(self, ledger):
        ledger["spend"] = {("principal", "month"): 48.0}
        before = _threshold("95", "principal", "month")

        await _check([PERSON_MONTH])

        assert _threshold("95", "principal", "month") == before + 1

    async def test_every_limit_that_crossed_one_is_counted_not_just_the_closest(self, ledger):
        ledger["spend"] = {("tenant", "day"): 7.5, ("principal", "day"): 4.8}
        tenant_before, person_before = _threshold("70"), _threshold("95", "principal", "day")

        await _check([TENANT_DAY, PERSON_DAY])

        assert _threshold("70") == tenant_before + 1
        assert _threshold("95", "principal", "day") == person_before + 1

    async def test_the_crossing_is_logged_once_per_day_not_on_every_turn_but_still_counted_every_turn(
        self, ledger, caplog
    ):
        ledger["spend"] = {("tenant", "day"): 9.0}
        before = _threshold("85")

        with caplog.at_level(logging.WARNING, logger=budgets.logger.name):
            for _ in range(3):
                await _check()

        assert [r.getMessage() for r in caplog.records].count("budget_threshold_crossed") == 1
        assert _threshold("85") == before + 3

    async def test_a_new_day_logs_the_crossing_again(self, ledger, caplog):
        ledger["spend"] = {("tenant", "day"): 9.0}

        with caplog.at_level(logging.WARNING, logger=budgets.logger.name):
            await budgets.check_allowance(TEST_CTX, limits=[TENANT_DAY], fail_policy="open", now=NOW)
            await budgets.check_allowance(
                TEST_CTX, limits=[TENANT_DAY], fail_policy="open", now=datetime(2026, 10, 16, 12, tzinfo=UTC)
            )

        assert [r.getMessage() for r in caplog.records].count("budget_threshold_crossed") == 2

    async def test_the_log_line_names_the_tenant_and_the_person(self, ledger, caplog):
        ledger["spend"] = {("tenant", "day"): 9.6}

        with caplog.at_level(logging.WARNING, logger=budgets.logger.name):
            await _check()

        record = next(r for r in caplog.records if r.getMessage() == "budget_threshold_crossed")
        assert (record.tenant, record.principal, record.threshold_pct) == (
            TEST_CTX["tenant"], TEST_CTX["principal"], "95",
        )

    async def test_a_refused_turn_counts_no_threshold(self, ledger):
        ledger["spend"] = {("tenant", "day"): 12.0}
        before = _threshold("95")

        await _check()

        assert _threshold("95") == before

    async def test_an_ok_result_reports_the_limit_closest_to_its_cap(self, ledger):
        ledger["spend"] = {("tenant", "day"): 1.0, ("principal", "day"): 4.0}

        allowance = await _check([TENANT_DAY, PERSON_DAY])

        assert (allowance.scope, allowance.window, allowance.spent_usd, allowance.limit_usd) == (
            "principal", "day", 4.0, 5.0,
        )


class TestWhenTheLedgerCannotBeRead:
    async def test_the_open_policy_serves_the_turn_but_says_it_was_not_verified(self, ledger):
        ledger["read_error"] = ConnectionError("appdata postgres unreachable")
        before = metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read")

        allowance = await _check(ALL_LIMITS, "open")

        assert allowance.status == "ok" and allowance.refused is False
        assert allowance.degraded is True
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read") == before + 1

    async def test_the_closed_policy_refuses_the_turn_as_unavailable_not_as_exceeded(self, ledger):
        ledger["read_error"] = ConnectionError("appdata postgres unreachable")
        degraded_before = metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read")
        exceeded_before = _exceeded()

        allowance = await _check(ALL_LIMITS, "closed")

        assert allowance.status == "unavailable" and allowance.refused is True
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="ledger_read") == degraded_before + 1
        # an unverifiable caller is not an over-budget one: the budget alerts must not fire on it
        assert _exceeded() == exceeded_before


class TestRefusalEnvelope:
    def test_a_tenant_daily_refusal_keeps_the_long_standing_code_and_message(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("exceeded"))

        assert envelope.code == errors.ErrorCode.TENANT_BUDGET_EXCEEDED
        assert envelope.message == "This tenant's daily usage budget has been reached. Please try again later."

    def test_a_tenant_monthly_refusal_says_monthly_and_when_it_resets(self):
        reset = datetime(2026, 11, 1, tzinfo=UTC)

        envelope = budgets.refusal_envelope(budgets.Allowance("exceeded", "tenant", "month", resets_at=reset))

        assert envelope.code == errors.ErrorCode.TENANT_BUDGET_EXCEEDED
        assert "monthly" in envelope.message
        assert envelope.details == {"scope": "tenant", "window": "month", "resets_at": reset.isoformat()}

    def test_a_personal_refusal_is_its_own_code_and_tells_the_person_it_is_theirs(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("exceeded", "principal", "day"))

        assert envelope.code == errors.ErrorCode.PERSONAL_BUDGET_EXCEEDED
        assert envelope.message.startswith("You have reached your personal daily")
        assert envelope.details == {"scope": "principal", "window": "day"}

    def test_an_unavailable_check_is_its_own_error_that_blames_no_budget(self):
        envelope = budgets.refusal_envelope(budgets.Allowance("unavailable"))

        assert envelope.code == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE
        assert "budget" not in envelope.message.lower()


class TestAsRuntimeWiresIt:
    async def test_the_configured_limits_are_read_from_the_runtime_module_at_call_time(self, ledger, monkeypatch):
        ledger["spend"] = {("principal", "day"): 6.0}

        assert await runtime_module._allowance_refusal(TEST_CTX) is None  # no personal limit configured

        monkeypatch.setattr(runtime_module, "MAX_COST_USD_PER_PRINCIPAL_PER_DAY", 5.0)
        refusal = await runtime_module._allowance_refusal(TEST_CTX)

        assert refusal is not None and refusal.code == errors.ErrorCode.PERSONAL_BUDGET_EXCEEDED

    async def test_the_failure_policy_is_read_from_the_runtime_module_at_call_time(self, ledger, monkeypatch):
        ledger["read_error"] = ConnectionError("down")

        monkeypatch.setattr(runtime_module, "BUDGET_CHECK_FAILURE_POLICY", "open")
        assert await runtime_module._allowance_refusal(TEST_CTX) is None

        monkeypatch.setattr(runtime_module, "BUDGET_CHECK_FAILURE_POLICY", "closed")
        refusal = await runtime_module._allowance_refusal(TEST_CTX)
        assert refusal is not None and refusal.code == errors.ErrorCode.BUDGET_CHECK_UNAVAILABLE

    async def test_a_turn_is_refused_before_any_graph_work_when_a_personal_limit_is_used_up(self, ledger, monkeypatch):
        ledger["spend"] = {("principal", "day"): 5.0}
        monkeypatch.setattr(runtime_module, "MAX_COST_USD_PER_PRINCIPAL_PER_DAY", 5.0)

        async def graph_must_not_be_touched(*args, **kwargs):
            raise AssertionError("a refused turn must never reach the graph")

        monkeypatch.setattr(runtime_module, "init_graph_async", graph_must_not_be_touched)

        events = [event async for event in stream_module.astream_events_turn("hi", "t1", TEST_CTX)]

        assert len(events) == 1
        assert events[0]["code"] == errors.ErrorCode.PERSONAL_BUDGET_EXCEEDED.value
        assert events[0]["details"]["scope"] == "principal"

    async def test_a_resume_is_refused_the_same_way(self, ledger, monkeypatch):
        ledger["spend"] = {("principal", "day"): 5.0}
        monkeypatch.setattr(runtime_module, "MAX_COST_USD_PER_PRINCIPAL_PER_DAY", 5.0)

        events = [event async for event in stream_module.astream_events_resume("t1", True, TEST_CTX)]

        assert events[0]["code"] == errors.ErrorCode.PERSONAL_BUDGET_EXCEEDED.value
