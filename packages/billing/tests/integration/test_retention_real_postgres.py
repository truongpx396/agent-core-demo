"""The billing-event retention sweep against a REAL Postgres (credit_billing/retention.py; the sibling of the agent app's, specs/010 T030c3,
moved by specs/013).

tests/test_retention.py proves the statements' shape and the loop through a scripted connection. What only a real database shows is what they
DO to the shipped schema, and the sweep's safety rests on it:

  * only events past the cutoff go, across batches, and a re-run deletes nothing more;
  * the append-only trigger lets the delete through only because the sweep says so inside its own transaction;
  * the foreign key from the export outbox does not block a `sent` event (both go together) and an event whose export never
    finished (`pending`, `failed`, `expired`) is KEPT and counted, whatever its age;
  * the wallet is untouched: a debit keeps its `usage_event_id` and the balance does not move.

The sweep spans tenants by design, and the container is shared across tests and xdist workers, so each test counts only ITS tenant's rows.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors as pg_errors

from credit_billing import credits, retention
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

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


async def purge(conn, *tenants: str) -> None:
    """Removes these tenants' events and the export rows that reference them. The table is append-only, but the SAME permission the sweep uses
    (local to this transaction) works for a cleanup: the sweep spans tenants and has a per-run ceiling, so events left behind would eat it."""
    await conn.execute("SELECT set_config('billing_usage_events.allow_delete', 'on', true)")
    await conn.execute("DELETE FROM billing_export_outbox WHERE tenant = ANY(%s)", (list(tenants),))
    await conn.execute("DELETE FROM billing_usage_events WHERE tenant = ANY(%s)", (list(tenants),))


@pytest.fixture
async def tenant(real_appdata) -> str:
    name = f"acme-{uuid.uuid4().hex[:8]}"
    yield name
    async with real_appdata() as conn:
        await purge(conn, name, f"{name}-other")


async def seed_event(conn, tenant: str, *, occurred_at: datetime | None = None) -> str:
    event_id = uuid.uuid4().hex
    await conn.execute(
        "INSERT INTO billing_usage_events (tenant, event_id, kind, cost_usd, occurred_at, recorded_at) "
        "VALUES (%s, %s, 'chat', 0.1, COALESCE(%s, now()), COALESCE(%s, now()))",
        (tenant, event_id, occurred_at, occurred_at),
    )
    return event_id


async def _events(conn, tenant: str) -> set[str]:
    cur = await conn.execute("SELECT event_id FROM billing_usage_events WHERE tenant = %s", (tenant,))
    return {row[0] for row in await cur.fetchall()}


async def _outbox(conn, tenant: str, event_id: str) -> list[str]:
    cur = await conn.execute("SELECT status FROM billing_export_outbox WHERE tenant = %s AND event_id = %s", (tenant, event_id))
    return [row[0] for row in await cur.fetchall()]


async def _queue(conn, tenant: str, event_id: str, status: str, provider: str = "p") -> None:
    """An outbox row in a given state. A `sent` row needs `sent_at` (a CHECK ties the two together)."""
    await conn.execute(
        "INSERT INTO billing_export_outbox (provider, tenant, event_id, status, sent_at) VALUES (%s, %s, %s, %s, %s)",
        (provider, tenant, event_id, status, NOW if status == "sent" else None),
    )


async def test_only_events_past_the_cutoff_go_and_a_rerun_deletes_nothing_more(tenant, real_appdata):
    async with real_appdata() as conn:
        old = {await seed_event(conn, tenant, occurred_at=OLD) for _ in range(5)}
        keep = {await seed_event(conn, tenant, occurred_at=INSIDE), await seed_event(conn, tenant)}

    # Batches of 2 (so ours take 2 + 2 + 1), with a ceiling far above the default: the sweep spans tenants, so anything old that another test
    # left behind is swept first, and the default ceiling must not decide this test's result.
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
            await conn.execute("DELETE FROM billing_usage_events WHERE tenant = %s AND event_id = %s", (tenant, event))

    await retention.sweep_old_events(older_than_days=400)
    async with real_appdata() as conn:
        assert event not in await _events(conn, tenant)


async def test_a_sent_event_goes_with_its_finished_export_row_instead_of_being_blocked_by_the_foreign_key(tenant, real_appdata):
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await _queue(conn, tenant, event, "sent")

    result = await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event not in await _events(conn, tenant) and await _outbox(conn, tenant, event) == []
    assert result.outbox_cleared >= 1


@pytest.mark.parametrize("status", ["pending", "failed", "expired"])
async def test_an_event_whose_export_never_finished_is_kept_whatever_its_age(tenant, real_appdata, status):
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await _queue(conn, tenant, event, status)

    result = await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event in await _events(conn, tenant) and await _outbox(conn, tenant, event) == [status]
    assert result.held_back >= 1, "kept events are counted so an operator can see them"


async def test_an_event_exported_to_two_providers_is_kept_until_both_are_finished(tenant, real_appdata):
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await _queue(conn, tenant, event, "sent", provider="a")
        await _queue(conn, tenant, event, "pending", provider="b")

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event in await _events(conn, tenant) and sorted(await _outbox(conn, tenant, event)) == ["pending", "sent"]


async def test_the_wallet_is_untouched_a_debit_keeps_its_event_id_and_the_balance_does_not_move(tenant, real_appdata):
    await credits.grant(tenant, 100, source="purchase", idempotency_key=f"g-{tenant}", actor="test")
    async with real_appdata() as conn:
        event = await seed_event(conn, tenant, occurred_at=OLD)
        await credits.debit_in(conn, tenant, 10, idempotency_key=event, usage_event_id=event, reason="usage (chat)")
    before = await credits.account_balance(tenant)

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert event not in await _events(conn, tenant)
        cur = await conn.execute("SELECT usage_event_id FROM credit_transactions WHERE tenant = %s AND idempotency_key = %s", (tenant, event))
        assert (await cur.fetchone())[0] == event, "lineage is kept even though the event row is gone"
    assert (await credits.account_balance(tenant)).available == before.available


async def test_the_same_event_id_under_another_tenant_is_a_different_event_and_is_not_swept_with_it(tenant, real_appdata):
    """The tenant is part of the key, so the sweep pairs an event with ITS export row and nothing else."""
    other = f"{tenant}-other"
    shared = uuid.uuid4().hex
    async with real_appdata() as conn:
        for who, when in ((tenant, OLD), (other, NOW)):
            await conn.execute(
                "INSERT INTO billing_usage_events (tenant, event_id, kind, occurred_at) VALUES (%s, %s, 'chat', %s)", (who, shared, when)
            )
        await _queue(conn, other, shared, "pending")

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert shared not in await _events(conn, tenant) and shared in await _events(conn, other)
        assert await _outbox(conn, other, shared) == ["pending"]


async def test_another_tenants_recent_events_are_never_touched(tenant, real_appdata):
    other = f"{tenant}-other"
    async with real_appdata() as conn:
        await seed_event(conn, tenant, occurred_at=OLD)
        recent = await seed_event(conn, other, occurred_at=NOW - timedelta(hours=1))

    await retention.sweep_old_events(older_than_days=400)

    async with real_appdata() as conn:
        assert recent in await _events(conn, other)
