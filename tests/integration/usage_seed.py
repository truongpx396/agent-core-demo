"""Seeds `usage_events` rows at an explicit time, for the integration tests that need spend from "two days ago".

The real writer (`usage_events._insert`) stamps `now()`, and the table is append-only (UPDATE is refused by a trigger),
so a test cannot write a row and age it afterwards the way tests/integration/test_ledger_real_postgres.py ages a ledger
row. Seeding with the time up front is the same thing without touching the immutability the table exists to guarantee."""
import uuid
from datetime import datetime


async def seed_event(
    conn,
    tenant: str,
    *,
    principal: str = "alice",
    cost_usd: float | None = 0.10,
    total_tokens: int = 100,
    occurred_at: datetime | None = None,
    event_id: str | None = None,
    kind: str = "chat",
) -> str:
    """Inserts one event on `conn` (a psycopg async connection) and returns its id. `cost_usd=None` is an unpriced call."""
    event_id = event_id or str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO usage_events (event_id, tenant, principal, thread_id, kind, model_alias, total_tokens, cost_usd, "
        "occurred_at, recorded_at) VALUES (%s, %s, %s, 'seed', %s, 'chat', %s, %s, COALESCE(%s, now()), COALESCE(%s, now()))",
        (event_id, tenant, principal, kind, total_tokens, cost_usd, occurred_at, occurred_at),
    )
    return event_id


async def seed_ledger_row(
    conn,
    tenant: str,
    *,
    principal: str = "alice",
    thread_id: str = "seed",
    total_tokens: int = 10,
    cost_usd: float = 0.01,
    recorded_at: datetime | None = None,
) -> None:
    """Inserts one `usage_ledger` row on `conn`. Nothing in the app writes this table any more (specs/010 T030c2: the caps
    sum the usage events), so the tests that still need a row, to prove the frozen table is no longer counted or to exercise
    its retention sweep, put it there directly."""
    await conn.execute(
        "INSERT INTO usage_ledger (tenant, principal, thread_id, model_alias, total_tokens, cost_usd, recorded_at) "
        "VALUES (%s, %s, %s, 'chat', %s, %s, COALESCE(%s, now()))",
        (tenant, principal, thread_id, total_tokens, cost_usd, recorded_at),
    )


async def purge_events(conn, *tenants: str) -> None:
    """Removes these tenants' usage events, and the export rows that reference them, when a test is done with them.

    The table is append-only, so a test cannot take back what it wrote the way it can delete a ledger row; but the SAME permission the
    retention sweep uses (`usage_events.allow_delete`, local to this transaction) works for a cleanup. It matters because the sweep
    spans tenants and has a per-run ceiling: a test that leaves thousands of years-old events behind (the carry-over's race test
    writes 3,000) makes a later sweep test hit that ceiling before it reaches its own rows. Found by exactly that failure."""
    await conn.execute("SELECT set_config('usage_events.allow_delete', 'on', true)")
    await conn.execute("DELETE FROM usage_export_outbox WHERE tenant = ANY(%s)", (list(tenants),))
    await conn.execute("DELETE FROM usage_events WHERE tenant = ANY(%s)", (list(tenants),))
