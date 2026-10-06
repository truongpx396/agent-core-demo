"""scripts/budget_policy.py — the operator CLI. Hermetic: the policy functions are replaced, so
these prove argument handling and what is asked of them (and that a mistake is rejected before
anything is written); tests/integration/test_budget_policies_real_postgres.py runs it for real."""
import pytest

from app.agent import budget_policies
from scripts import budget_policy


@pytest.fixture
def calls(monkeypatch):
    log = {"set": [], "clear": [], "closed": 0}

    async def set_override(tenant, subject, period, limit, by):
        log["set"].append((tenant, subject, period, limit, by))

    async def clear_override(tenant, subject, period):
        log["clear"].append((tenant, subject, period))
        return True

    async def list_overrides(tenant):
        return [budget_policies.Override("", "day", 50.0), budget_policies.Override("alice", "month", 0.0)]

    async def overrides_for(tenant, principal):
        return [budget_policies.Override("alice", "day", 25.0)]

    async def close_pool():
        log["closed"] += 1

    monkeypatch.setattr(budget_policies, "set_override", set_override)
    monkeypatch.setattr(budget_policies, "clear_override", clear_override)
    monkeypatch.setattr(budget_policies, "list_overrides", list_overrides)
    monkeypatch.setattr(budget_policies, "overrides_for", overrides_for)
    monkeypatch.setattr(budget_policy, "close_pool", close_pool)
    return log


async def test_set_a_tenant_limit(calls, capsys):
    code = await budget_policy.run(["set", "--tenant", "acme", "--period", "day", "--limit", "50", "--by", "ops"])

    assert code == 0
    assert calls["set"] == [("acme", "", "day", 50.0, "ops")]
    assert "$50" in capsys.readouterr().out


async def test_set_one_persons_limit(calls):
    await budget_policy.run(
        ["set", "--tenant", "acme", "--principal", "alice", "--period", "month", "--limit", "25", "--by", "ops"]
    )

    assert calls["set"] == [("acme", "alice", "month", 25.0, "ops")]


async def test_set_every_persons_limit_uses_the_star_subject(calls):
    await budget_policy.run(
        ["set", "--tenant", "acme", "--all-principals", "--period", "day", "--limit", "10", "--by", "ops"]
    )

    assert calls["set"] == [("acme", "*", "day", 10.0, "ops")]


async def test_a_limit_of_zero_suspends_and_says_so(calls, capsys):
    await budget_policy.run(
        ["set", "--tenant", "acme", "--principal", "mallory", "--period", "day", "--limit", "0", "--by", "ops"]
    )

    assert calls["set"] == [("acme", "mallory", "day", 0.0, "ops")]
    assert "suspended" in capsys.readouterr().out


async def test_a_limit_of_none_means_no_cap(calls, capsys):
    await budget_policy.run(["set", "--tenant", "internal", "--period", "month", "--limit", "NONE", "--by", "ops"])

    assert calls["set"] == [("internal", "", "month", None, "ops")]
    assert "no cap" in capsys.readouterr().out


async def test_clear_removes_one_override(calls):
    await budget_policy.run(["clear", "--tenant", "acme", "--principal", "alice", "--period", "day"])

    assert calls["clear"] == [("acme", "alice", "day")]


@pytest.mark.parametrize(
    "argv",
    [
        ["set", "--tenant", "acme", "--period", "day", "--limit", "-5", "--by", "ops"],
        ["set", "--tenant", "acme", "--period", "day", "--limit", "lots", "--by", "ops"],
        ["set", "--tenant", "acme", "--period", "week", "--limit", "5", "--by", "ops"],
        ["set", "--tenant", "acme", "--period", "day", "--limit", "5"],  # no --by: every change is attributed
        ["set", "--tenant", "acme", "--principal", "a", "--all-principals", "--period", "day", "--limit", "5", "--by", "o"],
    ],
)
async def test_a_mistake_is_rejected_before_anything_is_written(calls, argv):
    with pytest.raises(SystemExit):
        await budget_policy.run(argv)

    assert calls["set"] == []


@pytest.mark.parametrize("reserved", ["", "*"])
async def test_a_reserved_subject_cannot_be_used_as_a_principal_id(calls, reserved):
    with pytest.raises(SystemExit):
        await budget_policy.run(
            ["set", "--tenant", "acme", "--principal", reserved, "--period", "day", "--limit", "5", "--by", "ops"]
        )

    assert calls["set"] == []


async def test_show_lists_the_tenants_overrides_and_the_limits_that_apply_to_a_person(calls, capsys):
    await budget_policy.run(["show", "--tenant", "acme", "--principal", "alice"])

    out = capsys.readouterr().out
    assert "tenant" in out and "$50" in out and "suspended" in out
    assert "Limits that apply to 'alice'" in out
    assert "$25" in out  # alice's own day override beats the personal default


async def test_the_pool_is_closed_even_when_the_command_fails(calls, monkeypatch):
    async def boom(*args):
        raise ConnectionError("down")

    monkeypatch.setattr(budget_policies, "set_override", boom)

    with pytest.raises(ConnectionError):
        await budget_policy.run(["set", "--tenant", "acme", "--period", "day", "--limit", "5", "--by", "ops"])

    assert calls["closed"] == 1
