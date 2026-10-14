"""The usage-event retention sweep against a REAL Postgres (app/agent/usage_events_retention.py, specs/010 T030c3).

tests/agent/test_usage_events_retention.py proves the statements' shape and the loop through a scripted connection. What only a real
database shows is what they DO to the shipped schema, and the sweep's safety rests on it:

  * only events past the cutoff go, across batches, and a re-run deletes nothing more;
  * the append-only trigger lets the delete through only because the sweep says so inside its own transaction;
  * billing's own record of a call is untouched (it has its own retention, packages/billing/tests/integration/test_retention_real_postgres.py).

The sweep spans tenants by design, and the container is shared across tests and xdist workers, so this module is one xdist group
with the other modules that write old events (the carry-over and the spend tests), and each test counts only ITS tenant's rows.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors as pg_errors

from app.agent import usage_events_retention as retention
from tests.containers import ensure_postgres
from tests.integration.usage_seed import purge_events, seed_event

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("usage_events_old_rows")]

NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=500)  # past the default 400
INSIDE = NOW - timedelta(days=100)  # inside it


@pytest.fixture(scope="module")
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]


@pytest.fixture(autouse=True)
def real_appdata(appdata_url, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(appdata_url) as conn:
            yield conn

    monkeypatch.setattr(retention, "get_connection", get_connection)
    return get_connection


@pytest.fixture
async def tenant(real_appdata) -> str:
    """A tenant of this test's own. The events it writes are removed afterwards, so they do not
    accumulate across the session (see `purge_events`)."""
    name = f"acme-{uuid.uuid4().hex[:8]}"
    yield name
    async with real_appdata() as conn:
        await purge_events(conn, name, f"{name}-other")


async def _events(conn, tenant: str) -> set[str]:
    cur = await conn.execute("SELECT event_id FROM usage_events WHERE tenant = %s", (tenant,))
    return {row[0] for row in await cur.fetchall()}


async def test_only_events_past_the_cutoff_go_and_a_rerun_deletes_nothing_more(tenant, real_appdata):
    async with real_appdata() as conn:
        old = {await seed_event(conn, tenant, occurred_at=OLD) for _ in range(5)}
        keep = {await seed_event(conn, tenant, occurred_at=INSIDE), await seed_event(conn, tenant)}

    # Batches of 2 (so ours take 2 + 2 + 1), with a ceiling far above the default: the sweep spans tenants, so anything old that
    # another test left behind is swept first, and the default ceiling (2,000 rows at this size) must not decide this test's result.
    first = await retention.sweep_old_events(older_than_days=400, batch_size=2, max_batches=100_000)
    second = await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert await _events(conn, tenant) == keep
    assert first.deleted >= 5 and first.complete is True
    assert second.deleted == 0, "a re-run before more events age out deletes nothing"
    assert old.isdisjoint(keep)


async def test_the_floor_protects_a_window_something_still_reads(tenant, real_appdata):
    async with real_appdata() as conn:
        month_old = await seed_event(conn, tenant, occurred_at=NOW - timedelta(days=40))

    with pytest.raises(ValueError, match="floor"):
        await retention.sweep_old_events(older_than_days=30)

    async with real_appdata() as conn:
        assert month_old in await _events(conn, tenant)


async def test_the_table_still_refuses_a_delete_that_does_not_say_so(tenant, real_appdata):
    """The sweep is allowed to delete only because it says so inside its own transaction: a bare DELETE is still refused."""
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)

    async with real_appdata() as conn:
        with pytest.raises(pg_errors.RaiseException, match="append-only"):
            await conn.execute("DELETE FROM usage_events WHERE event_id = %s", (event,))

    await retention.sweep_old_events(older_than_days=400)
    async with real_appdata() as conn:
        assert event not in await _events(conn, tenant)


async def test_another_tenants_recent_events_are_never_touched(tenant, real_appdata):
    other = f"{tenant}-other"
    async with real_appdata() as conn:
        await seed_event(conn, tenant, occurred_at=OLD)
        recent = await seed_event(conn, other, occurred_at=NOW - timedelta(hours=1))

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert recent in await _events(conn, other)
