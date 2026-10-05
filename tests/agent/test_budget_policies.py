"""Per-tenant and per-person limit overrides (app/agent/budget_policies.py, budgets.resolve_limits).

Hermetic: `get_connection` is a fake, so these prove the precedence rules, the statement shape
(tenant in every WHERE) and the failure behaviour — not what Postgres does with the table, which
tests/integration/test_budget_policies_real_postgres.py runs for real.

The autouse `mock_budget_policies` fixture replaces `overrides_for` for every other test;
`_REAL_OVERRIDES_FOR` is the real one, captured before any fixture runs.
"""
import logging
from contextlib import asynccontextmanager

import pytest
from psycopg import errors as pg_errors

from app.agent import budget_policies, budgets
from app.agent import runtime as runtime_module
from app.core import errors, metrics
from tests.conftest import TEST_CTX, metric_value

_REAL_OVERRIDES_FOR = budget_policies.overrides_for

O = budget_policies.Override  # noqa: N817 - a table of rows reads better with a short name
DEFAULTS = budgets.Defaults(tenant_day=20.0, tenant_month=400.0, principal_day=5.0, principal_month=100.0)


def _limits(overrides, principal="alice", defaults=DEFAULTS):
    return {(l.scope, l.window): l.limit_usd for l in budgets.resolve_limits(defaults, overrides, principal)}


class TestResolveLimits:
    def test_with_no_overrides_the_settings_defaults_apply_in_order(self):
        limits = budgets.resolve_limits(DEFAULTS, [], "alice")

        assert [(l.scope, l.window, l.limit_usd) for l in limits] == [
            ("tenant", "day", 20.0),
            ("tenant", "month", 400.0),
            ("principal", "day", 5.0),
            ("principal", "month", 100.0),
        ]

    def test_a_tenant_row_replaces_the_tenant_default_for_that_period_only(self):
        assert _limits([O("", "day", 50.0)]) == {
            ("tenant", "day"): 50.0,
            ("tenant", "month"): 400.0,
            ("principal", "day"): 5.0,
            ("principal", "month"): 100.0,
        }

    def test_a_person_row_beats_the_tenant_wide_personal_row_which_beats_the_default(self):
        overrides = [O("*", "day", 10.0), O("alice", "day", 25.0)]

        assert _limits(overrides, "alice")[("principal", "day")] == 25.0
        assert _limits(overrides, "bob")[("principal", "day")] == 10.0  # only the '*' row names bob
        assert _limits([], "bob")[("principal", "day")] == 5.0

    def test_another_persons_row_never_applies(self):
        """`overrides_for` only fetches the caller's rows, but resolve_limits must also be safe
        on its own: a row for someone else is not this person's limit."""
        assert _limits([O("mallory", "day", 0.0)], "alice")[("principal", "day")] == 5.0

    def test_a_none_override_is_an_explicit_no_cap_that_beats_a_default(self):
        limits = _limits([O("", "month", None), O("*", "day", None)])

        assert ("tenant", "month") not in limits
        assert ("principal", "day") not in limits
        assert ("tenant", "day") in limits and ("principal", "month") in limits

    def test_even_the_always_on_tenant_daily_limit_can_be_explicitly_uncapped(self):
        assert ("tenant", "day") not in _limits([O("", "day", None)])

    def test_zero_is_a_real_cap_that_suspends_not_a_missing_one(self):
        limits = _limits([O("alice", "day", 0.0)])

        assert limits[("principal", "day")] == 0.0

    def test_an_override_can_switch_on_a_limit_the_defaults_leave_off(self):
        off = budgets.Defaults(tenant_day=20.0)

        assert _limits([O("*", "day", 7.0)], defaults=off)[("principal", "day")] == 7.0
        assert ("principal", "day") not in _limits([], defaults=off)

    def test_configured_limits_is_resolve_limits_with_no_overrides(self):
        assert budgets.configured_limits(tenant_day=20.0, principal_day=5.0) == budgets.resolve_limits(
            budgets.Defaults(tenant_day=20.0, principal_day=5.0), [], ""
        )


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    async def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.calls = list(rows), error, []

    async def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if self.error:
            raise self.error
        return _Cursor(self.rows)


def _use(monkeypatch, conn):
    @asynccontextmanager
    async def get_connection():
        yield conn

    monkeypatch.setattr(budget_policies, "get_connection", get_connection)


@pytest.fixture
def real_reads(monkeypatch):
    monkeypatch.setattr(budget_policies, "overrides_for", _REAL_OVERRIDES_FOR)


class TestOverridesFor:
    async def test_reads_only_this_tenants_rows_for_the_three_subjects_that_can_apply(self, monkeypatch, real_reads):
        conn = _Conn()
        _use(monkeypatch, conn)

        await budget_policies.overrides_for("acme", "alice")

        sql, params = conn.calls[0]
        assert "WHERE tenant = %s AND subject IN (%s, %s, %s)" in sql
        assert params == ("acme", "", "*", "alice")

    async def test_returns_rows_with_none_kept_distinct_from_zero(self, monkeypatch, real_reads):
        _use(monkeypatch, _Conn([("", "day", None), ("alice", "month", 0), ("*", "day", 12.5)]))

        rows = await budget_policies.overrides_for("acme", "alice")

        assert rows == [O("", "day", None), O("alice", "month", 0.0), O("*", "day", 12.5)]

    async def test_a_read_is_cached_per_tenant_and_person_until_it_expires(self, monkeypatch, real_reads):
        conn = _Conn([("alice", "day", 1.0)])
        _use(monkeypatch, conn)
        monkeypatch.setattr(budget_policies, "BUDGET_POLICY_REFRESH_SECONDS", 30)

        for _ in range(3):
            await budget_policies.overrides_for("acme", "alice")
        await budget_policies.overrides_for("acme", "bob")  # a different person is a different read
        await budget_policies.overrides_for("other", "alice")  # and so is a different tenant

        assert len(conn.calls) == 3

    async def test_a_cache_of_zero_seconds_reads_every_time(self, monkeypatch, real_reads):
        conn = _Conn()
        _use(monkeypatch, conn)
        monkeypatch.setattr(budget_policies, "BUDGET_POLICY_REFRESH_SECONDS", 0)

        await budget_policies.overrides_for("acme", "alice")
        await budget_policies.overrides_for("acme", "alice")

        assert len(conn.calls) == 2

    async def test_a_database_error_is_raised_for_the_caller_to_apply_the_failure_policy(self, monkeypatch, real_reads):
        _use(monkeypatch, _Conn(error=ConnectionError("appdata postgres unreachable")))

        with pytest.raises(ConnectionError):
            await budget_policies.overrides_for("acme", "alice")

    async def test_a_failed_read_is_not_cached(self, monkeypatch, real_reads):
        conn = _Conn(error=ConnectionError("down"))
        _use(monkeypatch, conn)

        for _ in range(2):
            with pytest.raises(ConnectionError):
                await budget_policies.overrides_for("acme", "alice")

        assert len(conn.calls) == 2

    async def test_a_missing_table_reads_as_no_overrides_and_warns_once(self, monkeypatch, real_reads, caplog):
        """The migration is applied by hand on an existing volume; until it is, this must not
        take every turn down (and, under the closed policy, must not refuse them all)."""
        monkeypatch.setattr(budget_policies, "BUDGET_POLICY_REFRESH_SECONDS", 0)
        _use(monkeypatch, _Conn(error=pg_errors.UndefinedTable("relation budget_policies does not exist")))

        with caplog.at_level(logging.WARNING, logger=budget_policies.logger.name):
            assert await budget_policies.overrides_for("acme", "alice") == []
            assert await budget_policies.overrides_for("acme", "alice") == []

        assert sum("table is missing" in r.getMessage() for r in caplog.records) == 1


class TestWriting:
    async def test_set_upserts_one_row_and_records_who(self, monkeypatch):
        conn = _Conn()
        _use(monkeypatch, conn)

        await budget_policies.set_override("acme", "alice", "day", 25.0, "ops@example.com")

        sql, params = conn.calls[0]
        assert "ON CONFLICT (tenant, subject, period) DO UPDATE" in sql
        assert params == ("acme", "alice", "day", 25.0, "ops@example.com")

    async def test_set_accepts_none_for_no_cap_and_zero_for_suspend(self, monkeypatch):
        conn = _Conn()
        _use(monkeypatch, conn)

        await budget_policies.set_override("acme", "", "month", None, "ops")
        await budget_policies.set_override("acme", "mallory", "day", 0.0, "ops")

        assert [c[1][3] for c in conn.calls] == [None, 0.0]

    @pytest.mark.parametrize(
        ("period", "limit", "tenant", "by"),
        [("week", 1.0, "acme", "ops"), ("day", -1.0, "acme", "ops"), ("day", 1.0, "", "ops"), ("day", 1.0, "acme", "")],
    )
    async def test_set_rejects_bad_input_before_touching_the_database(self, monkeypatch, period, limit, tenant, by):
        async def fail(*args, **kwargs):
            raise AssertionError("nothing may be written for invalid input")

        monkeypatch.setattr(budget_policies, "get_connection", fail)

        with pytest.raises(ValueError):
            await budget_policies.set_override(tenant, "alice", period, limit, by)

    async def test_clear_deletes_exactly_one_row_scoped_to_the_tenant(self, monkeypatch):
        conn = _Conn([("x",)])
        _use(monkeypatch, conn)

        assert await budget_policies.clear_override("acme", "alice", "day") is True

        sql, params = conn.calls[0]
        assert "WHERE tenant = %s AND subject = %s AND period = %s" in sql
        assert params == ("acme", "alice", "day")

    async def test_clearing_a_row_that_does_not_exist_says_so(self, monkeypatch):
        _use(monkeypatch, _Conn())

        assert await budget_policies.clear_override("acme", "alice", "day") is False


@pytest.fixture
def ledger(monkeypatch):
    spend = {}

    async def usage_summary(tenant, principal=None, since=None):
        return {"total_cost_usd": spend.get("principal" if principal else "tenant", 0.0), "total_tokens": 0}

    async def in_flight_reservation(tenant):
        return 0.0

    from app.agent import usage_ledger

    monkeypatch.setattr(usage_ledger, "usage_summary", usage_summary)
    monkeypatch.setattr(usage_ledger, "in_flight_reservation", in_flight_reservation)
    return spend


def _overrides(monkeypatch, rows=None, error=None):
    async def overrides_for(tenant, principal):
        if error:
            raise error
        return rows or []

    monkeypatch.setattr(budget_policies, "overrides_for", overrides_for)


class TestCheckAppliesTheOverrides:
    async def test_a_suspended_person_is_refused_while_a_colleague_is_not(self, monkeypatch, ledger):
        _overrides(monkeypatch, [O("alice", "day", 0.0)])
        colleague = {**TEST_CTX, "principal": "bob"}

        alice = await budgets.check(TEST_CTX | {"principal": "alice"}, defaults=DEFAULTS, fail_policy="open")
        bob = await budgets.check(colleague, defaults=DEFAULTS, fail_policy="open")

        assert alice.status == "exceeded" and alice.scope == "principal"
        assert bob.status == "ok"

    async def test_an_override_raises_a_limit_above_the_default(self, monkeypatch, ledger):
        ledger["principal"] = 6.0  # past the $5 default
        _overrides(monkeypatch, [O("*", "day", 20.0)])

        assert (await budgets.check(TEST_CTX, defaults=DEFAULTS, fail_policy="open")).status == "ok"

    async def test_a_tenant_override_lowers_the_tenant_limit(self, monkeypatch, ledger):
        ledger["tenant"] = 3.0
        _overrides(monkeypatch, [O("", "day", 3.0)])

        allowance = await budgets.check(TEST_CTX, defaults=DEFAULTS, fail_policy="open")

        assert (allowance.status, allowance.scope, allowance.limit_usd) == ("exceeded", "tenant", 3.0)

    async def test_the_overrides_are_read_for_this_tenant_and_person(self, monkeypatch, ledger):
        seen = []

        async def overrides_for(tenant, principal):
            seen.append((tenant, principal))
            return []

        monkeypatch.setattr(budget_policies, "overrides_for", overrides_for)

        await budgets.check(TEST_CTX, defaults=DEFAULTS, fail_policy="open")

        assert seen == [(TEST_CTX["tenant"], TEST_CTX["principal"])]

    async def test_an_unattributable_ctx_is_ok_without_reading_anything(self, monkeypatch):
        async def fail(*args):
            raise AssertionError("nothing to look up for an invalid ctx")

        monkeypatch.setattr(budget_policies, "overrides_for", fail)

        assert (await budgets.check(None, defaults=DEFAULTS, fail_policy="closed")).status == "ok"


class TestWhenTheOverridesCannotBeRead:
    async def test_the_open_policy_serves_the_turn_on_the_defaults_and_says_it_was_not_fully_verified(
        self, monkeypatch, ledger
    ):
        _overrides(monkeypatch, error=ConnectionError("down"))
        before = metric_value(metrics.agent_cost_governance_degraded_total, path="policy_read")

        allowance = await budgets.check(TEST_CTX, defaults=DEFAULTS, fail_policy="open")

        assert allowance.status == "ok" and allowance.degraded is True
        assert metric_value(metrics.agent_cost_governance_degraded_total, path="policy_read") == before + 1

    async def test_the_defaults_still_apply_while_the_overrides_are_unreadable(self, monkeypatch, ledger):
        ledger["tenant"] = 25.0  # past the $20 default
        _overrides(monkeypatch, error=ConnectionError("down"))

        assert (await budgets.check(TEST_CTX, defaults=DEFAULTS, fail_policy="open")).status == "exceeded"

    async def test_the_closed_policy_refuses_the_turn_as_unavailable(self, monkeypatch, ledger):
        _overrides(monkeypatch, error=ConnectionError("down"))

        allowance = await budgets.check(TEST_CTX, defaults=DEFAULTS, fail_policy="closed")

        assert allowance.status == "unavailable"


class TestAsRuntimeWiresIt:
    async def test_a_suspension_reaches_a_turn_as_a_personal_refusal(self, monkeypatch, ledger):
        _overrides(monkeypatch, [O(TEST_CTX["principal"], "day", 0.0)])

        refusal = await runtime_module._allowance_refusal(TEST_CTX)

        assert refusal is not None
        assert refusal.code == errors.ErrorCode.PERSONAL_BUDGET_EXCEEDED
