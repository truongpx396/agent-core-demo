"""Real usage/cost ledger (GRAPH_PATTERNS.md pattern 26) — closes the "no
Meter, real or hollow" gap: a shipped standalone-product default is
supposed to keep an actual usage ledger, not a no-op that makes a broken
deployment look configured (the same "no hollow defaults" principle
app/agent/moderation.py's real (if narrowly-scoped) check already follows).

Persisted in the same `appdata` Postgres database app/agent/sql_store.py already
uses (`usage_ledger` table, postgres-init/03-meter.sql) — a separate table,
not a separate database, since it's the same "this app's own operational
data" lifecycle as `employees`. Every row is tenant+principal scoped, the
same isolation axis every other write in this app already carries.

Cost is computed from a small, explicit, approximate per-model price table
(`PRICE_PER_1K_TOKENS_USD`) — $0 for any model alias not listed, which is
every locally-run Ollama model this demo's own docker-compose ships (local
inference has no per-token API cost). Tokens are still recorded
unconditionally regardless of whether a price is known, so pointing
`OPENAI_API_BASE` at a real paid provider and adding its alias to the price
table is the only change needed for real cost tracking — the ledger's
shape never changes.
"""
import logging
from datetime import datetime

from app.agent.model_resolver import resolve_model
from app.agent.sql_store import get_connection
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

# Approximate, illustrative USD-per-1000-tokens pricing — update to match
# your actual provider's current rates. An alias absent from this table
# costs $0 (true today for every model this app's docker-compose runs
# locally via Ollama through LiteLLM).
PRICE_PER_1K_TOKENS_USD: dict[str, float] = {
    "gpt-4o": 0.005,
    "gpt-4o-mini": 0.00015,
}


def record_usage(
    ctx: SecurityCtx | None, thread_id: str, model_alias: str, total_tokens: int
) -> None:
    """Best-effort write-through after a turn completes (app/agent/runtime.py's
    `_record_turn_metrics`). A ledger write that fails must not fail the
    turn whose usage it's trying to record — same degrade-don't-crash
    posture as app/retrieval/semantic_cache.py's `set()`. Records nothing (and logs)
    without a valid ctx or with zero tokens — a usage row with no
    tenant/principal is unattributable, and a zero-token row (a rejected/
    short-circuited turn) has nothing to meter.
    """
    if not valid_ctx(ctx) or total_tokens <= 0:
        return
    price_per_1k = PRICE_PER_1K_TOKENS_USD.get(model_alias, 0.0)
    cost_usd = (total_tokens / 1000) * price_per_1k
    # The resolved CONCRETE model behind `model_alias` (GRAPH_PATTERNS.md
    # pattern 38) — None if resolution itself degrades (LiteLLM
    # unreachable, alias unknown); never blocks the write.
    resolved_model = resolve_model(model_alias)
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO usage_ledger "
                "(tenant, principal, thread_id, model_alias, total_tokens, cost_usd, resolved_model) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    ctx["tenant"],
                    ctx["principal"],
                    thread_id,
                    model_alias,
                    total_tokens,
                    cost_usd,
                    resolved_model,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "usage ledger write failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


def usage_summary(
    tenant: str, principal: str | None = None, since: datetime | None = None
) -> dict:
    """Real read path proving the ledger isn't write-only: total tokens
    and cost for `tenant`, optionally narrowed to one `principal` — never
    the other way around (no way to query across tenants) — and/or to
    usage recorded on or after `since`. `since` is what
    `_tenant_over_daily_budget` (app/agent/runtime.py) uses for a ROLLING
    24-hour-window budget check (`since = now - 24h`, not calendar-day
    boundaries, so the window a request is checked against never resets a
    tenant's near-limit status mid-day the way a midnight-UTC boundary
    would) — `usage_summary`'s own all-time default (`since=None`) stays
    the right shape for `GET /usage`'s "how much has this tenant ever
    spent" question, a different one."""
    where = ["tenant = %s"]
    params: list = [tenant]
    if principal:
        where.append("principal = %s")
        params.append(principal)
    if since is not None:
        where.append("recorded_at >= %s")
        params.append(since)

    sql = (
        "SELECT COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
        f"FROM usage_ledger WHERE {' AND '.join(where)}"
    )
    with get_connection() as conn:
        cur = conn.execute(sql, params)
        total_tokens, total_cost = cur.fetchone()
    return {"total_tokens": int(total_tokens), "total_cost_usd": float(total_cost)}
