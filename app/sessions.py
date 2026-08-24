"""Session directory for the web UI's session switcher (item #9 of the
production-hardening roadmap) — lets a user list and switch between their
own past conversation threads.

Persisted in the same `appdata` Postgres database app/sql_store.py and
app/meter.py already use (`chat_sessions` table, postgres-init/06-chat-sessions.sql)
— this app's own operational-data lifecycle, not a new database. A
dedicated table rather than reusing `usage_ledger`: that table no-ops on
a zero-token/rejected turn (app/meter.py::record_usage) and carries no
title, so a session with no billable tokens (a moderation block, a cache
hit) would silently vanish from a "resume my conversations" list built
from it. The checkpointer's own `checkpoints` table has `thread_id` but
no tenant/principal column at all — not scoped to an owner, and
deserializing a title preview out of LangGraph's internal checkpoint blob
format from application code would be fragile across a
langgraph-checkpoint-postgres version bump.

Every row is tenant+principal scoped, the same isolation axis every other
write in this app already carries (app/security.py's SecurityCtx).
"""
import logging

from app.security import SecurityCtx, valid_ctx
from app.sql_store import get_connection

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60


def upsert_session(ctx: SecurityCtx | None, thread_id: str, title: str | None = None) -> None:
    """Best-effort write-through at the start of every turn (app/agent.py's
    stream_turn/answer/astream_events_turn, right after seeding) — NOT
    gated on the turn actually completing or producing any tokens, unlike
    app/meter.py::record_usage, specifically so a rejected/moderated/
    short-circuited turn still shows up in the session list (the user did
    start a real conversation on this thread_id, whatever happened next).

    `title` is set ONLY on first insert (the ON CONFLICT clause below
    never touches it) — later turns on the same thread_id only refresh
    `last_active_at`, so the session's displayed name stays whatever its
    opening message was, not the most recent one. A ledger write that
    fails must not fail the turn whose session it's trying to record —
    same degrade-don't-crash posture as record_usage.
    """
    if not valid_ctx(ctx) or not thread_id:
        return
    display_title = (title or "New conversation").strip() or "New conversation"
    if len(display_title) > TITLE_MAX_CHARS:
        display_title = display_title[:TITLE_MAX_CHARS].rstrip() + "…"
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()",
                (thread_id, ctx["tenant"], ctx["principal"], display_title),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat session upsert failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


def list_sessions(ctx: SecurityCtx | None) -> list[dict]:
    """Every session belonging to ctx's tenant+principal, most recently
    active first. Never scoped to tenant alone — a session belongs to
    whoever started it, the same owner-level isolation
    app/security.py::Policy.lower already applies to memories (as opposed
    to documents, which are tenant-shared)."""
    if not valid_ctx(ctx):
        return []
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT thread_id, title, created_at, last_active_at FROM chat_sessions "
            "WHERE tenant = %s AND principal = %s ORDER BY last_active_at DESC",
            (ctx["tenant"], ctx["principal"]),
        )
        columns = [desc.name for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def session_belongs_to(ctx: SecurityCtx | None, thread_id: str) -> bool:
    """Ownership check for GET /chat/sessions/{thread_id}/messages —
    app/agent.py::get_session_messages reads the shared Postgres
    checkpointer directly, which carries no tenant/principal of its own
    to check against (see this module's docstring), so the CALLER must
    verify ownership here first, against this table, before ever reading
    that thread_id's transcript. A single targeted row lookup, not
    `thread_id in {s["thread_id"] for s in list_sessions(ctx)}` — no
    reason to fetch every session just to check membership of one."""
    if not valid_ctx(ctx) or not thread_id:
        return False
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s AND principal = %s",
            (thread_id, ctx["tenant"], ctx["principal"]),
        )
        return cur.fetchone() is not None
