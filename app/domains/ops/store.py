"""Fixed, parameterized queries against `ops_incidents`
(postgres-init/10-ops-incidents.sql) — the ops bot's incident log, so a
confirmed anomaly can be recorded durably instead of only posted as a
one-off team-channel message. No generated SQL; reuses sql_store's pooled
`appdata` connection, same discipline as support/store.py.

Deliberately NOT `tenant`-scoped, unlike support_tickets/crm_leads: this
app's own operational metrics have no per-tenant dimension to inherit
(see postgres-init/10-ops-incidents.sql's comment).
"""
from app.agent.sql_store import get_connection


async def log_incident(opened_by: str, summary: str, detail: str | None) -> int:
    """Insert one new incident, always `status='open'`. Returns the new
    incident's id (fresh, never caller-targeted)."""
    sql = (
        "INSERT INTO ops_incidents (opened_by, summary, detail) "
        "VALUES (%s, %s, %s) RETURNING id"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [opened_by, summary, detail])
        (incident_id,) = await cur.fetchone()
        return int(incident_id)


async def list_recent_incidents(limit: int = 10, status: str | None = None) -> list[dict]:
    """Most recent incidents first, optionally narrowed to one `status`
    ('open'/'resolved'), so an investigation can check "has this happened
    before?" without digging through team-channel history."""
    sql = (
        "SELECT id, opened_by, summary, detail, status, resolution, created_at, resolved_at "
        "FROM ops_incidents"
    )
    params: list = []
    if status is not None:
        sql += " WHERE status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


async def resolve_incident(incident_id: int, resolution: str) -> bool:
    """Marks an incident resolved. Returns False if no incident with that
    id exists, so the tool impl can tell the model "no such incident"
    instead of silently no-op-ing."""
    sql = (
        "UPDATE ops_incidents SET status = 'resolved', resolution = %s, resolved_at = now() "
        "WHERE id = %s"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, [resolution, incident_id])
        return cur.rowcount > 0
