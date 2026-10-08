"""The frozen usage ledger against a REAL Postgres (spec 008 A9, A3; retired as a write path in specs/010 T030c2).

Nothing writes `usage_ledger` any more: the dollar caps sum the usage events. What is left to prove against the shipped
`postgres-init/*.sql` schema is the retention sweep of the frozen table (until it, too, is retired in T030c3):

  * it deletes only rows past the cutoff, across batches, and leaves every recent row alone;
  * `17-usage-ledger-indexes.sql` applies on a fresh volume.

The rolling-window READ that used to be here (`usage_summary`, and the proof that its index serves the allowance query)
moved with the dollar caps to the usage events: see tests/integration/test_spend_real_postgres.py (specs/010 T030).
The ledger's write was tested here too (`record_usage`); it is gone with the function.

"Time passing" is simulated by moving `recorded_at` into the past, not by sleeping.
Each test uses its own tenant; the container is shared across tests and xdist
workers, and the sweep spans tenants by design, so this module is one xdist group.
"""
import uuid
from contextlib import asynccontextmanager

import psycopg
import pytest

from app.agent import usage_ledger
from tests.containers import ensure_postgres
from tests.integration.usage_seed import seed_ledger_row

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


async def test_sweep_deletes_only_rows_past_the_cutoff_and_across_batches(tenant, real_appdata):
    other = f"{tenant}-other"
    async with real_appdata() as conn:
        for i in range(5):
            await seed_ledger_row(conn, tenant, thread_id=f"old-{i}")
    await _age(real_appdata, tenant, days=100)
    async with real_appdata() as conn:
        await seed_ledger_row(conn, tenant, thread_id="recent")
        await seed_ledger_row(conn, other, thread_id="other-recent")

    deleted = await usage_ledger.sweep_old_rows(older_than_days=90, batch_size=2)  # 2 + 2 + 1

    assert deleted >= 5  # the table is shared; at least our five old rows went
    assert await _row_count(real_appdata, tenant) == 1
    assert await _row_count(real_appdata, other) == 1  # recent rows are never touched, in any tenant


async def test_sweep_leaves_a_row_just_inside_the_window(tenant, real_appdata):
    async with real_appdata() as conn:
        await seed_ledger_row(conn, tenant, thread_id="edge")
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
