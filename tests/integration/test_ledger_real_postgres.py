"""The usage ledger against a REAL Postgres (spec 008 A9, A3).

tests/agent/test_record_usage.py and test_usage_ledger.py pin the SQL the ledger
sends through a fake cursor. That proves statement shape, not what the real table
does — and the real table is where this module has been wrong before (the budget
reservation, spec 008 B20, passed every fake-cursor test and still leaked). These
run `record_usage`, `usage_summary` and `sweep_old_rows` against the shipped
`postgres-init/*.sql` schema:

  * a row lands with the tenant, principal, tokens and cost the caller gave;
  * the rolling-window read honours `since`, and never crosses tenants or, when
    narrowed, principals — the property every budget decision depends on;
  * the retention sweep deletes only rows past the cutoff, across batches, and
    leaves every recent row alone;
  * `17-usage-ledger-indexes.sql` applies on a fresh volume and the allowance read
    can actually use its index.

"Time passing" is simulated by moving `recorded_at` into the past, not by sleeping.
Each test uses its own tenant; the container is shared across tests and xdist
workers, and the sweep spans tenants by design, so this module is one xdist group.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.agent import usage_ledger
from tests.containers import ensure_postgres

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("usage_ledger_real_postgres")]


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    """Point the ledger at the real database (its OWN `get_connection` binding —
    tests/conftest.py's autouse `mock_appdata_postgres` patches the same name)."""

    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    monkeypatch.setattr(usage_ledger, "get_connection", get_connection)
    return get_connection


@pytest.fixture
def tenant() -> str:
    return f"acme-{uuid.uuid4().hex[:8]}"


def _ctx(tenant: str, principal: str = "alice") -> dict:
    return {"tenant": tenant, "principal": principal, "claims": {}}


async def _age(real_appdata, tenant: str, days: float, principal: str | None = None) -> None:
    async with real_appdata() as conn:
        await conn.execute(
            "UPDATE usage_ledger SET recorded_at = recorded_at - make_interval(secs => %s) "
            "WHERE tenant = %s AND (%s::text IS NULL OR principal = %s)",
            (days * 86400, tenant, principal, principal),
        )


async def _row_count(real_appdata, tenant: str) -> int:
    async with real_appdata() as conn:
        cur = await conn.execute("SELECT count(*) FROM usage_ledger WHERE tenant = %s", (tenant,))
        return (await cur.fetchone())[0]


async def test_record_usage_writes_one_row_with_what_the_caller_gave(tenant, real_appdata):
    await usage_ledger.record_usage(_ctx(tenant, "alice"), "thread-1", "chat", 1500, 0.0075)

    async with real_appdata() as conn:
        cur = await conn.execute(
            "SELECT principal, thread_id, model_alias, total_tokens, cost_usd FROM usage_ledger WHERE tenant = %s",
            (tenant,),
        )
        rows = await cur.fetchall()

    assert [(r[0], r[1], r[2], r[3], float(r[4])) for r in rows] == [("alice", "thread-1", "chat", 1500, 0.0075)]


async def test_record_usage_writes_nothing_for_no_identity_or_no_tokens(tenant, real_appdata):
    await usage_ledger.record_usage(None, "thread-1", "chat", 500, 0.1)
    await usage_ledger.record_usage(_ctx(tenant), "thread-1", "chat", 0, 0.1)

    assert await _row_count(real_appdata, tenant) == 0


async def test_usage_summary_sums_only_this_tenant(tenant):
    other = f"{tenant}-other"
    await usage_ledger.record_usage(_ctx(tenant), "t1", "chat", 100, 0.10)
    await usage_ledger.record_usage(_ctx(tenant), "t2", "chat", 200, 0.20)
    await usage_ledger.record_usage(_ctx(other), "t3", "chat", 9999, 9.99)

    summary = await usage_ledger.usage_summary(tenant)

    assert summary["total_tokens"] == 300
    assert summary["total_cost_usd"] == pytest.approx(0.30)


async def test_usage_summary_narrows_to_one_principal_inside_a_tenant(tenant):
    await usage_ledger.record_usage(_ctx(tenant, "alice"), "t1", "chat", 100, 0.10)
    await usage_ledger.record_usage(_ctx(tenant, "bob"), "t2", "chat", 700, 0.70)

    assert (await usage_ledger.usage_summary(tenant, principal="alice"))["total_cost_usd"] == pytest.approx(0.10)
    assert (await usage_ledger.usage_summary(tenant))["total_cost_usd"] == pytest.approx(0.80)


async def test_usage_summary_since_is_a_rolling_window_not_a_calendar_day(tenant, real_appdata):
    await usage_ledger.record_usage(_ctx(tenant), "old", "chat", 100, 5.00)
    await _age(real_appdata, tenant, days=2)
    await usage_ledger.record_usage(_ctx(tenant), "new", "chat", 100, 0.50)
    since = datetime.now(UTC) - timedelta(hours=24)

    in_window = await usage_ledger.usage_summary(tenant, since=since)
    all_time = await usage_ledger.usage_summary(tenant)

    assert in_window["total_cost_usd"] == pytest.approx(0.50)
    assert all_time["total_cost_usd"] == pytest.approx(5.50)


async def test_sweep_deletes_only_rows_past_the_cutoff_and_across_batches(tenant, real_appdata):
    other = f"{tenant}-other"
    for i in range(5):
        await usage_ledger.record_usage(_ctx(tenant), f"old-{i}", "chat", 10, 0.01)
    await _age(real_appdata, tenant, days=100)
    await usage_ledger.record_usage(_ctx(tenant), "recent", "chat", 10, 0.01)
    await usage_ledger.record_usage(_ctx(other), "other-recent", "chat", 10, 0.01)

    deleted = await usage_ledger.sweep_old_rows(older_than_days=90, batch_size=2)  # 2 + 2 + 1

    assert deleted >= 5  # the table is shared; at least our five old rows went
    assert await _row_count(real_appdata, tenant) == 1
    assert await _row_count(real_appdata, other) == 1  # recent rows are never touched, in any tenant


async def test_sweep_leaves_a_row_just_inside_the_window(tenant, real_appdata):
    await usage_ledger.record_usage(_ctx(tenant), "edge", "chat", 10, 0.01)
    await _age(real_appdata, tenant, days=89.9)

    await usage_ledger.sweep_old_rows(older_than_days=90)

    assert await _row_count(real_appdata, tenant) == 1


async def test_the_shipped_schema_has_the_window_indexes_and_not_the_redundant_one(real_appdata):
    async with real_appdata() as conn:
        cur = await conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'usage_ledger'")
        names = {row[0] for row in await cur.fetchall()}

    assert "usage_ledger_tenant_recorded_at_idx" in names
    assert "usage_ledger_tenant_principal_recorded_at_idx" in names
    assert "usage_ledger_tenant_principal_idx" not in names  # a prefix of the new one


async def test_the_allowance_read_can_use_its_index(tenant, real_appdata):
    """With sequential scans disabled the planner must pick the (tenant,
    recorded_at) index for the allowance query — proving the index serves that
    exact predicate, not merely that it exists. (On a table this small the
    planner would otherwise choose a scan on cost alone, which says nothing.)"""
    async with real_appdata() as conn:
        await conn.execute("SET enable_seqscan = off")
        cur = await conn.execute(
            "EXPLAIN SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
            "FROM usage_ledger WHERE tenant = %s AND recorded_at >= %s",
            (tenant, datetime.now(UTC) - timedelta(hours=24)),
        )
        plan = "\n".join(row[0] for row in await cur.fetchall())

    assert "usage_ledger_tenant_recorded_at_idx" in plan
