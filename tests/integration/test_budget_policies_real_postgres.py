"""Limit overrides against a REAL Postgres (postgres-init/18-budget-policies.sql).

tests/agent/test_budget_policies.py proves the precedence rules and statement shape through a fake
cursor. What it cannot show is what the table itself does, and three properties live there:

  * NULL ("no cap") and 0 ("suspend") must come back as different things — they are opposite
    instructions, and a driver or a column type that collapsed them would turn a suspension into
    an exemption;
  * the CHECK constraints refuse a nonsense period or a negative limit even from someone who
    bypasses the Python validation;
  * the shipped migration applies cleanly on a fresh volume.

The last test runs the whole path with real usage events: suspending one person refuses that person's
turns and leaves a colleague's alone.

Each test uses its own tenant; the container is shared across tests and xdist workers.
"""
import uuid
from contextlib import asynccontextmanager

import psycopg
import pytest

from app.agent import budget_holds, budget_policies, budgets, spend
from scripts import budget_policy
from tests.containers import ensure_postgres
from tests.integration.usage_seed import seed_event

pytestmark = pytest.mark.integration

_REAL_OVERRIDES_FOR = budget_policies.overrides_for
DEFAULTS = budgets.Defaults(tenant_day=20.0, tenant_month=0.0, principal_day=5.0, principal_month=0.0)


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    for module in (budget_policies, budget_holds, spend):
        monkeypatch.setattr(module, "get_connection", get_connection)
    monkeypatch.setattr(budget_policies, "overrides_for", _REAL_OVERRIDES_FOR)
    monkeypatch.setattr(budget_policies, "BUDGET_POLICY_REFRESH_SECONDS", 0)
    return get_connection


@pytest.fixture
def tenant() -> str:
    return f"acme-{uuid.uuid4().hex[:8]}"


def _ctx(tenant: str, principal: str) -> dict:
    return {"tenant": tenant, "principal": principal, "claims": {}}


async def test_none_and_zero_come_back_as_different_instructions(tenant):
    await budget_policies.set_override(tenant, "", "month", None, "test")  # no cap
    await budget_policies.set_override(tenant, "alice", "day", 0.0, "test")  # suspend

    rows = {(o.subject, o.period): o.limit_usd for o in await budget_policies.list_overrides(tenant)}

    assert rows[("", "month")] is None
    assert rows[("alice", "day")] == 0.0 and rows[("alice", "day")] is not None


async def test_setting_the_same_row_again_replaces_it_and_records_the_new_operator(tenant, real_appdata):
    await budget_policies.set_override(tenant, "alice", "day", 5.0, "first")
    await budget_policies.set_override(tenant, "alice", "day", 9.0, "second")

    async with real_appdata() as conn:
        cur = await conn.execute(
            "SELECT limit_usd, updated_by FROM budget_policies WHERE tenant = %s AND subject = 'alice'", (tenant,)
        )
        rows = await cur.fetchall()

    assert [(float(limit), by) for limit, by in rows] == [(9.0, "second")]


async def test_overrides_for_returns_only_this_tenants_rows_for_this_person(tenant):
    other = f"{tenant}-other"
    await budget_policies.set_override(tenant, "", "day", 50.0, "test")
    await budget_policies.set_override(tenant, "*", "day", 10.0, "test")
    await budget_policies.set_override(tenant, "alice", "day", 25.0, "test")
    await budget_policies.set_override(tenant, "bob", "day", 1.0, "test")  # someone else's
    await budget_policies.set_override(other, "alice", "day", 0.0, "test")  # another tenant's alice

    rows = await budget_policies.overrides_for(tenant, "alice")

    assert sorted((o.subject, o.limit_usd) for o in rows) == [("", 50.0), ("*", 10.0), ("alice", 25.0)]


async def test_clearing_restores_the_default_and_reports_whether_a_row_existed(tenant):
    await budget_policies.set_override(tenant, "alice", "day", 0.0, "test")

    assert await budget_policies.clear_override(tenant, "alice", "day") is True
    assert await budget_policies.clear_override(tenant, "alice", "day") is False
    assert await budget_policies.overrides_for(tenant, "alice") == []


@pytest.mark.parametrize(
    ("period", "limit"),
    [("week", 1.0), ("", 1.0), ("day", -0.01)],
)
async def test_the_table_itself_refuses_a_bad_period_or_a_negative_limit(tenant, real_appdata, period, limit):
    """Bypassing the Python validation: the CHECK constraints are the last line."""
    async with real_appdata() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "INSERT INTO budget_policies (tenant, subject, period, limit_usd, updated_by) VALUES (%s, 'x', %s, %s, 't')",
                (tenant, period, limit),
            )


async def test_the_cli_round_trips_against_the_real_table(tenant, monkeypatch, capsys):
    async def no_close():
        return None

    monkeypatch.setattr(budget_policy, "close_pool", no_close)

    await budget_policy.run(["set", "--tenant", tenant, "--principal", "mallory", "--period", "day", "--limit", "0", "--by", "ops"])
    await budget_policy.run(["show", "--tenant", tenant, "--principal", "mallory"])
    shown = capsys.readouterr().out
    await budget_policy.run(["clear", "--tenant", tenant, "--principal", "mallory", "--period", "day"])

    assert "suspended" in shown
    assert await budget_policies.overrides_for(tenant, "mallory") == []


async def test_a_suspension_refuses_that_person_and_not_a_colleague_end_to_end(tenant):
    """Real ledger, real policies, no fakes: alice has spent nothing and is refused anyway
    because an operator suspended her; bob is untouched."""
    await budget_policies.set_override(tenant, "alice", "day", 0.0, "ops")

    alice = await budgets.check(_ctx(tenant, "alice"), defaults=DEFAULTS, fail_policy="closed")
    bob = await budgets.check(_ctx(tenant, "bob"), defaults=DEFAULTS, fail_policy="closed")

    assert (alice.status, alice.scope) == ("exceeded", "principal")
    assert bob.status == "ok" and bob.degraded is False


async def test_a_real_event_spend_trips_a_personal_override_not_the_default(tenant, real_appdata):
    await budget_policies.set_override(tenant, "alice", "day", 0.50, "ops")
    async with real_appdata() as conn:
        await seed_event(conn, tenant, principal="alice", cost_usd=0.60, total_tokens=1000)

    alice = await budgets.check(_ctx(tenant, "alice"), defaults=DEFAULTS, fail_policy="closed")
    statuses = await budgets.usage_status(_ctx(tenant, "alice"), defaults=DEFAULTS)

    assert alice.status == "exceeded" and alice.limit_usd == 0.50
    personal = next(s for s in statuses if (s.scope, s.window) == ("principal", "day"))
    assert (personal.limit_usd, personal.spent_usd, personal.remaining_usd) == (0.50, pytest.approx(0.60), 0.0)
