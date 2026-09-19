"""Session directory for the web UI's session switcher — lets a user list
and switch between their own past conversation threads.

Persisted in the `appdata` Postgres database (`chat_sessions` table,
postgres-init/06-chat-sessions.sql), not reused from `usage_ledger`: that
table no-ops on a zero-token/rejected turn and carries no title, so a
session with no billable tokens would vanish from the switcher. The
checkpointer's own `checkpoints` table has no tenant/principal column to
scope by, and parsing a title out of its internal blob format would be
fragile across version bumps.

Every row is tenant+principal scoped (SecurityCtx) and, since pattern 49,
domain-scoped too: a session belongs to whichever domain actually ran on
it, since resuming under a DIFFERENT domain would run that domain's
tools/prompt against history never built around them (see
`job_queue/queue.py::publish_request` for the same hazard). `domain`
defaults to `"ecorp"` and is set ONLY on first insert — immutable after,
like `title`.
"""
import logging

from app.agent.sql_store import get_connection
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60


async def upsert_session(
    ctx: SecurityCtx | None, thread_id: str, title: str | None = None, domain: str = "ecorp"
) -> None:
    """Best-effort write-through at the start of every turn
    (`runtime.py::astream_events_turn`, right after seeding) — NOT gated
    on the turn completing, unlike `usage_ledger.py::record_usage`, so a
    rejected/moderated/short-circuited turn still shows up in the session
    list.

    `title`/`domain` are set ONLY on first insert; later turns only
    refresh `last_active_at`, so both stay fixed at the opening turn's
    values. A failing write must not fail the turn — same
    degrade-don't-crash posture as record_usage.
    """
    if not valid_ctx(ctx) or not thread_id:
        return
    display_title = (title or "New conversation").strip() or "New conversation"
    if len(display_title) > TITLE_MAX_CHARS:
        display_title = display_title[:TITLE_MAX_CHARS].rstrip() + "…"
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()",
                (thread_id, ctx["tenant"], ctx["principal"], display_title, domain),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat session upsert failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


async def list_sessions(ctx: SecurityCtx | None, domain: str = "ecorp") -> list[dict]:
    """Every session for ctx's tenant+principal AND `domain`, most
    recently active first. Owner-level isolation (like `Policy.lower` for
    memories, not tenant-shared documents), with domain as a third
    scoping axis: the same principal's "support" and "sales" conversations
    never appear in each other's switcher."""
    if not valid_ctx(ctx):
        return []
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id, title, created_at, last_active_at FROM chat_sessions "
            "WHERE tenant = %s AND principal = %s AND domain = %s ORDER BY last_active_at DESC",
            (ctx["tenant"], ctx["principal"], domain),
        )
        columns = [desc.name for desc in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]


async def session_belongs_to(ctx: SecurityCtx | None, thread_id: str, domain: str = "ecorp") -> bool:
    """Ownership check for GET /chat/sessions/{thread_id}/messages —
    `runtime_stream.py::get_session_messages` reads the shared Postgres
    checkpointer directly, which has no tenant/principal/domain to check,
    so the CALLER must verify ownership here first. A targeted row
    lookup, not a full `list_sessions` scan. Requiring `domain` to match
    too means a thread opened under a different domain reads as "not
    found," same as a different tenant's or principal's thread."""
    if not valid_ctx(ctx) or not thread_id:
        return False
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s "
            "AND principal = %s AND domain = %s",
            (thread_id, ctx["tenant"], ctx["principal"], domain),
        )
        return await cur.fetchone() is not None
