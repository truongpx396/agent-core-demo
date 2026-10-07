"""Hand-made wallet changes against a REAL Postgres: `credits.adjust_in` and the operator CLI that calls it (scripts/credits.py).

A correction made by hand is the entry nobody can re-derive from a usage event, so what holds it together is database behaviour: the
idempotency key that makes a retried command harmless, the lock that serialises it with the debits racing it, the append-only entries a
reviewer reads afterwards. Stated and proven here (constitution VII), with the CLI driven end to end so the whole path (parse, actor,
key, wallet, ledger, `show`) is one test. The wallet's own rules are tests/integration/test_credits_real_postgres.py.

Each test uses its own tenant: the container is shared across tests and xdist workers.
"""
import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal

import psycopg
import pytest

from app.agent import sql_store
from app.billing import credits
from app.core import metrics
from scripts import credits as cli
from tests.conftest import metric_value
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

D = Decimal


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:  # commits on a normal exit
            yield conn

    monkeypatch.setattr(credits, "get_connection", get_connection)
    monkeypatch.setattr(sql_store, "get_connection", get_connection)  # the CLI's `show` reads through it


def tenant_name() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


async def scalar(url, sql, *params):
    async with await psycopg.AsyncConnection.connect(url) as conn:
        cur = await conn.execute(sql, params)
        return (await cur.fetchone())[0]


async def run(*argv: str) -> tuple[int, str]:
    return await cli.execute(cli.build_parser().parse_args(argv))


async def assert_consistent(url, tenant):
    assert await credits.verify(tenant) == []
    assert (await credits.balance(tenant)).ledger == await scalar(url, "SELECT COALESCE(SUM(amount), 0) FROM credit_entries WHERE tenant = %s", tenant)


class TestAPositiveAdjustment:
    async def test_it_adds_a_lot_marked_as_an_adjustment_and_repays_debt_first(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, 10, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        await credits.debit(tenant, 25, idempotency_key=f"d-{tenant}", actor="test")  # 15 of debt

        applied = await credits.adjust(tenant, 20, idempotency_key=f"a-{tenant}", actor="operator:alice", reason="credit for the outage")

        balance = await credits.balance(tenant)
        assert applied.applied and (balance.debt, balance.available) == (D("0"), D("5"))
        assert await scalar(appdata_url, "SELECT source FROM credit_lots WHERE tenant = %s AND granted = 20", tenant) == "adjustment"
        assert await scalar(appdata_url, "SELECT kind FROM credit_transactions WHERE id = %s", applied.transaction_id) == "grant"
        await assert_consistent(appdata_url, tenant)

    async def test_it_opens_a_wallet_for_a_tenant_that_has_none_like_any_grant(self, appdata_url):
        tenant = tenant_name()

        await credits.adjust(tenant, 3, idempotency_key=f"a-{tenant}", actor="operator:alice", reason="goodwill")

        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_accounts WHERE tenant = %s", tenant) == 1


class TestANegativeAdjustment:
    async def test_it_takes_credits_back_as_kind_adjust_and_is_never_refused_for_a_short_balance(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, 10, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        overdrafts = metric_value(metrics.agent_credit_overdraft_total)

        applied = await credits.adjust(tenant, -25, idempotency_key=f"a-{tenant}", actor="operator:alice", reason="call billed twice")

        balance = await credits.balance(tenant)
        assert applied.applied and applied.shortfall == D("15")
        assert (balance.available, balance.debt) == (D("-15"), D("15"))  # the correction is not blocked by the very mistake it fixes
        assert await scalar(appdata_url, "SELECT kind FROM credit_transactions WHERE id = %s", applied.transaction_id) == "adjust"
        assert metric_value(metrics.agent_credit_overdraft_total) == overdrafts, "an operator's correction is not usage outrunning the wallet"
        await assert_consistent(appdata_url, tenant)

    async def test_a_tenant_with_no_wallet_gets_none_opened_to_hold_a_debt(self, appdata_url):
        tenant = tenant_name()

        applied = await credits.adjust(tenant, -5, idempotency_key=f"a-{tenant}", actor="operator:alice", reason="r")

        assert applied.status == "no_account"
        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_accounts WHERE tenant = %s", tenant) == 0


class TestARetry:
    async def test_the_same_key_changes_nothing_twice_in_either_direction(self, appdata_url):
        tenant = tenant_name()
        await credits.grant(tenant, 100, source="purchase", idempotency_key=f"g-{tenant}", actor="test")

        first = await credits.adjust(tenant, -10, idempotency_key="fix-1", actor="operator:alice", reason="r")
        again = await credits.adjust(tenant, -10, idempotency_key="fix-1", actor="operator:alice", reason="r")
        plus = await credits.adjust(tenant, 4, idempotency_key="fix-2", actor="operator:alice", reason="r")
        plus_again = await credits.adjust(tenant, 4, idempotency_key="fix-2", actor="operator:alice", reason="r")

        assert (again.status, plus_again.status) == ("duplicate", "duplicate")
        assert again.transaction_id == first.transaction_id and plus_again.transaction_id == plus.transaction_id
        assert (await credits.balance(tenant)).available == D("94")


class TestTheCounters:
    async def test_grants_and_debits_are_counted_by_source_and_kind(self):
        tenant = tenant_name()
        granted = lambda s: metric_value(metrics.agent_credit_granted_total, source=s)  # noqa: E731 - a one-line reader for this test
        debited = lambda k: metric_value(metrics.agent_credit_debited_total, kind=k)  # noqa: E731
        before = {**{s: granted(s) for s in ("purchase", "adjustment")}, **{k: debited(k) for k in ("debit", "adjust", "clawback")}}

        await credits.grant(tenant, 50, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
        await credits.adjust(tenant, 5, idempotency_key=f"a1-{tenant}", actor="o", reason="r")
        await credits.debit(tenant, 7, idempotency_key=f"d-{tenant}", actor="test")
        await credits.adjust(tenant, -3, idempotency_key=f"a2-{tenant}", actor="o", reason="r")
        await credits.debit(tenant, 2, idempotency_key=f"c-{tenant}", actor="test", kind="clawback")
        await credits.debit(tenant, 7, idempotency_key=f"d-{tenant}", actor="test")  # a replay: counted once

        assert {s: granted(s) - before[s] for s in ("purchase", "adjustment")} == {"purchase": 50, "adjustment": 5}
        assert {k: debited(k) - before[k] for k in ("debit", "adjust", "clawback")} == {"debit": 7, "adjust": 3, "clawback": 2}


class TestTheCommandEndToEnd:
    async def test_a_grant_then_show_tells_who_what_and_why(self, appdata_url):
        tenant = tenant_name()

        code, text = await run("grant", "--tenant", tenant, "--amount", "500", "--by", "alice", "--reason", "pilot top-up, ticket 4412", "--key", "k-1")

        assert code == 0 and "Idempotency key: k-1" in text
        code, shown = await run("show", "--tenant", tenant)
        assert code == 0
        assert "available 500" in shown and "consistent" in shown and "manual" in shown
        assert "operator:alice" in shown and "pilot top-up, ticket 4412" in shown

    async def test_a_retry_with_the_key_is_a_duplicate_and_without_it_a_second_grant(self, appdata_url):
        tenant = tenant_name()
        base = ["grant", "--tenant", tenant, "--amount", "10", "--by", "alice", "--reason", "r"]

        await run(*base, "--key", "same")
        code, text = await run(*base, "--key", "same")
        assert code == 0 and "Already applied under key same" in text and (await credits.balance(tenant)).available == D("10")

        await run(*base)
        assert (await credits.balance(tenant)).available == D("20")

    async def test_a_promo_without_an_expiry_is_refused_and_opens_no_wallet(self, appdata_url):
        tenant = tenant_name()

        code, text = await run("grant", "--tenant", tenant, "--amount", "5", "--source", "promo", "--by", "alice", "--reason", "launch")

        assert code == 2 and "must expire" in text
        assert await scalar(appdata_url, "SELECT COUNT(*) FROM credit_accounts WHERE tenant = %s", tenant) == 0

    async def test_a_promo_with_an_expiry_shows_it(self):
        tenant = tenant_name()

        await run("grant", "--tenant", tenant, "--amount", "5", "--source", "promo", "--expires-in-days", "30", "--by", "alice", "--reason", "launch")
        _, shown = await run("show", "--tenant", tenant)

        assert "promo" in shown and "expires 20" in shown

    async def test_an_adjustment_through_the_command_can_leave_debt_and_show_says_so(self):
        tenant = tenant_name()
        await run("grant", "--tenant", tenant, "--amount", "10", "--by", "alice", "--reason", "r")

        code, text = await run("adjust", "--tenant", tenant, "--amount=-25", "--by", "bob", "--reason", "double charge")
        _, shown = await run("show", "--tenant", tenant)

        assert code == 0 and "15 of it is now debt" in text
        assert "debt 15" in shown and "operator:bob" in shown and "double charge" in shown and "adjust" in shown

    async def test_show_for_a_tenant_with_no_wallet_says_so(self):
        code, text = await run("show", "--tenant", tenant_name())

        assert code == 1 and "no credit wallet" in text

    async def test_show_never_lists_another_tenants_lots_or_entries(self):
        mine, theirs = tenant_name(), tenant_name()
        await run("grant", "--tenant", mine, "--amount", "11", "--by", "alice", "--reason", "mine-reason")
        await run("grant", "--tenant", theirs, "--amount", "22", "--by", "carol", "--reason", "their-reason")

        _, shown = await run("show", "--tenant", mine)
        _, as_json = await run("show", "--tenant", mine, "--json")

        assert "their-reason" not in shown and "carol" not in shown and "22" not in shown
        data = json.loads(as_json)
        assert [lot["granted"] for lot in data["lots"]] == ["11.000000"]
        assert {entry["reason"] for entry in data["entries"]} == {"mine-reason"}

    async def test_the_entry_limit_is_bounded_and_newest_first(self):
        tenant = tenant_name()
        for i in range(5):
            await run("grant", "--tenant", tenant, "--amount", "1", "--by", "alice", "--reason", f"reason-{i}")

        _, as_json = await run("show", "--tenant", tenant, "--entries", "2", "--json")
        _, huge = await run("show", "--tenant", tenant, "--entries", "100000", "--json")

        assert [e["reason"] for e in json.loads(as_json)["entries"]] == ["reason-4", "reason-3"]
        assert len(json.loads(huge)["entries"]) == 5  # clamped to the maximum, which is above what exists
