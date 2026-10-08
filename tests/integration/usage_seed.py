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
