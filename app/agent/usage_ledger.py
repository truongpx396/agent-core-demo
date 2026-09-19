"""Real usage/cost ledger (pattern 26) — closes the "no hollow Meter" gap:
a shipped default should keep an actual ledger, not a no-op that makes a
broken deployment look configured (same principle as moderation.py's real
check).

Persisted in the same `appdata` Postgres database as sql_store.py
(`usage_ledger` table, postgres-init/03-meter.sql). Every row is
tenant+principal scoped.

Cost comes from a small, explicit per-model price table
(`PRICE_PER_1K_TOKENS_USD`) — $0 for any unlisted alias, true for every
Ollama model this demo runs locally. Tokens are recorded unconditionally
regardless of price, so pointing at a real paid provider and adding its
alias is the only change needed for real cost tracking.
"""
import logging
from datetime import datetime

from app.agent.model_resolver import resolve_model
from app.agent.sql_store import get_connection
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

# Approximate, illustrative USD/1000-token pricing — update to match your
# provider's rates. An alias absent from this table costs $0 (true for
# every model this app runs locally via Ollama/LiteLLM).
PRICE_PER_1K_TOKENS_USD: dict[str, float] = {
    "gpt-4o": 0.005,
    "gpt-4o-mini": 0.00015,
}


async def record_usage(
    ctx: SecurityCtx | None, thread_id: str, model_alias: str, total_tokens: int
) -> None:
    """Best-effort write-through after a turn completes
    (`runtime.py::_record_turn_metrics`). A failing write must not fail
    the turn it's recording — same degrade-don't-crash posture as
    `semantic_cache.py::set()`. No-ops without a valid ctx or with zero
    tokens (unattributable / nothing to meter).
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
        async with get_connection() as conn:
            await conn.execute(
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


async def usage_summary(
    tenant: str, principal: str | None = None, since: datetime | None = None
) -> dict:
    """Total tokens and cost for `tenant`, optionally narrowed to one
    `principal` and/or to usage on/after `since`. `since` is what
    `_tenant_over_daily_budget` (runtime.py) uses for a ROLLING 24h window
    (`now - 24h`, not calendar-day boundaries, so a tenant's near-limit
    status never resets mid-day). `since=None` (all-time) is the right
    default for `GET /usage`'s "total ever spent" question instead."""
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
    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        total_tokens, total_cost = await cur.fetchone()
    return {"total_tokens": int(total_tokens), "total_cost_usd": float(total_cost)}
