"""Fixed, parameterized queries against `ops_incidents`
(postgres-init/10-ops-incidents.sql) — the ops bot's own incident log, so an
investigation that finds a real anomaly can record it durably instead of
only ever posting a one-off team-channel message that scrolls away. Same
discipline as app/agent/sql_store.py/app/domains/support/store.py: no
generated SQL, reusing that module's own pooled `appdata` connection.

Deliberately NOT `tenant`-scoped, unlike support_tickets/crm_leads — see
postgres-init/10-ops-incidents.sql's own comment for why: this app's own
operational metrics have no per-tenant dimension for an incident about them
to inherit.
"""
from app.agent.sql_store import get_connection


def log_incident(opened_by: str, summary: str, detail: str | None) -> int:
    """Insert one new incident, always `status='open'` — the only way an
    incident comes into existence, mirroring
    app/domains/support/store.py::create_ticket's "a fresh id, never a
    caller-targeted one" discipline. Returns the new incident's id."""
    sql = (
        "INSERT INTO ops_incidents (opened_by, summary, detail) "
        "VALUES (%s, %s, %s) RETURNING id"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [opened_by, summary, detail])
        (incident_id,) = cur.fetchone()
        return int(incident_id)


def list_recent_incidents(limit: int = 10, status: str | None = None) -> list[dict]:
    """Most recent incidents first, optionally narrowed to one `status`
    ('open'/'resolved') — what `list_recent_incidents` shows so an
    investigation can check "has this happened before?" without a human
    having to dig through team-channel history."""
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
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def resolve_incident(incident_id: int, resolution: str) -> bool:
    """Marks an incident resolved — returns False (no update applied) if no
    incident with that id exists, so the tool impl can tell the model "no
    such incident" instead of silently no-op-ing, same contract as
    app/domains/support/store.py::escalate_ticket."""
    sql = (
        "UPDATE ops_incidents SET status = 'resolved', resolution = %s, resolved_at = now() "
        "WHERE id = %s"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, [resolution, incident_id])
        return cur.rowcount > 0
