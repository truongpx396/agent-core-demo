"""The usage-event retention sweep against a REAL Postgres (app/agent/usage_events_retention.py, specs/010 T030c3).

tests/agent/test_usage_events_retention.py proves the statements' shape and the loop through a scripted connection. What only a real
database shows is what they DO to the shipped schema, and the sweep's safety rests on it:

  * only events past the cutoff go, across batches, and a re-run deletes nothing more;
  * the append-only trigger lets the delete through only because the sweep says so inside its own transaction;
  * the foreign key from the export outbox does not block a `sent` event (both go together) and an event whose export never
    finished (`pending`, `failed`, `expired`) is KEPT and counted, whatever its age;
  * the wallet is untouched: a debit keeps its `usage_event_id` and the balance does not move.

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
from app.billing import credits
from tests.containers import ensure_postgres
from tests.integration.usage_seed import purge_events, seed_event

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("usage_ledger_real_postgres")]

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
    monkeypatch.setattr(credits, "get_connection", get_connection)
    return get_connection


@pytest.fixture
async def tenant(real_appdata) -> str:
    """A tenant of this test's own. The events it keeps on purpose (unfinished exports) are removed afterwards, so they do not
    accumulate across the session or collide with a test that clears the outbox (see `purge_events`)."""
    name = f"acme-{uuid.uuid4().hex[:8]}"
    yield name
    async with real_appdata() as conn:
        await purge_events(conn, name, f"{name}-other")


async def _events(conn, tenant: str) -> set[str]:
    cur = await conn.execute("SELECT event_id FROM usage_events WHERE tenant = %s", (tenant,))
    return {row[0] for row in await cur.fetchall()}


async def _outbox(conn, event_id: str) -> list[str]:
    cur = await conn.execute("SELECT status FROM usage_export_outbox WHERE event_id = %s", (event_id,))
    return [row[0] for row in await cur.fetchall()]


async def _queue(conn, tenant: str, event_id: str, status: str, provider: str = "p") -> None:
    """An outbox row in a given state. A `sent` row needs `sent_at` (a CHECK ties the two together)."""
    await conn.execute(
        "INSERT INTO usage_export_outbox (provider, event_id, tenant, status, sent_at) VALUES (%s, %s, %s, %s, %s)",
        (provider, event_id, tenant, status, NOW if status == "sent" else None),
    )


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


async def test_a_sent_event_goes_with_its_finished_export_row_instead_of_being_blocked_by_the_foreign_key(tenant, real_appdata):
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await _queue(conn, tenant, event, "sent")

    result = await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event not in await _events(conn, tenant) and await _outbox(conn, event) == []
    assert result.outbox_cleared >= 1


@pytest.mark.parametrize("status", ["pending", "failed", "expired"])
async def test_an_event_whose_export_never_finished_is_kept_whatever_its_age(tenant, real_appdata, status):
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await _queue(conn, tenant, event, status)

    result = await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event in await _events(conn, tenant) and await _outbox(conn, event) == [status]
    assert result.held_back >= 1, "kept events are counted so an operator can see them"


async def test_an_event_exported_to_two_providers_is_kept_until_both_are_finished(tenant, real_appdata):
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await _queue(conn, tenant, event, "sent", provider="a")
        await _queue(conn, tenant, event, "pending", provider="b")

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event in await _events(conn, tenant) and sorted(await _outbox(conn, event)) == ["pending", "sent"]


async def test_the_wallet_is_untouched_a_debit_keeps_its_event_id_and_the_balance_does_not_move(tenant, real_appdata):
    await credits.grant(tenant, 100, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await credits.debit_in(conn, tenant, 10, idempotency_key=event, usage_event_id=event, reason="model call (chat)")
    before = await credits.account_balance(tenant)

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event not in await _events(conn, tenant)
        cur = await conn.execute("SELECT usage_event_id FROM credit_transactions WHERE tenant = %s AND idempotency_key = %s", (tenant, event))
        assert (await cur.fetchone())[0] == event, "lineage is kept even though the event row is gone"
    assert (await credits.account_balance(tenant)).available == before.available


async def test_another_tenants_recent_events_are_never_touched(tenant, real_appdata):
    other = f"{tenant}-other"
    async with real_appdata() as conn:
        await seed_event(conn, tenant, occurred_at=OLD)
        recent = await seed_event(conn, other, occurred_at=NOW - timedelta(hours=1))

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert recent in await _events(conn, other)
