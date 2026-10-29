"""One immutable usage event per model call (postgres-init/19-usage-events.sql): the meter the dollar caps and
`GET /usage` sum (`spend.py`).

Identity: `event_id = uuid5(NAMESPACE, f"{tenant}|{message_id}")`, with `message_id` the response's
`AIMessage.id`. langchain-core assigns `run-<run id>-<index>`, unique per invocation and stable across a
checkpoint round trip (specs/010 R5, pinned by tests/agent/cost/test_usage_events.py). The provider's
`chatcmpl-...` id is not used: a backend may return a fixed or missing one. A LangGraph node retry that calls
the model again is a second paid call and gets a new event.

Failure policy: fail OPEN, counted, alerted, since a failed write mustn't fail the turn but is spend no cap
sees. `agent_cost_governance_degraded_total{path=...}`: `usage_event_write` -> UsageEventWriteFailing
(critical); `usage_event_table_missing` (migration 19 not applied) -> UsageEventTableMissing;
`usage_event_identity` (no message id, so a random one that can't be de-duplicated); `usage_missing` (the
provider reported no usage; counted only, a local model may do this). There is no kill switch, since switching
writes off would make every cap read $0 (`USAGE_EVENTS_ENABLED=false` is refused at startup).

Billing is one call and not this module's business: after inserting its row, on the same connection and
transaction and only for a newly inserted row, it hands the billing package ONE plain value (tenant, event id,
kind, cost, model, tokens) through `app/billing_integration/port.py`. What the package does with it is its own
(`packages/billing/src/credit_billing/usage.py`); the table has no billing column.

The per-turn `usage_ledger` is gone (specs/010 T030c); `scripts/usage_events_carry_over.py` copied its history
here.
"""
import logging
import uuid

from app.agent.cost.pricing import PricedCall
from app.core import metrics
from app.core.security import SecurityCtx, valid_ctx
from app.ports import cost as ports
from app.ports import models as model_ports
from app.ports.cost import UsageEventRow, UsageTableMissing

logger = logging.getLogger(__name__)

# The closed set the table's CHECK enforces. Embeddings and cron arrive with the PRs that route them here.
KINDS = frozenset({"chat", "followups", "compaction", "subagent", "embedding", "cron"})

_EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-core-demo/usage-event")
_warned_missing_table = False


def event_id_for(tenant: str, message_id: str) -> str:
    """The deterministic id of one call. 36 characters, inside Stripe's 100-character
    `identifier` limit, and a pure function of its inputs so every retry agrees."""
    return str(uuid.uuid5(_EVENT_NAMESPACE, f"{tenant}|{message_id}"))


def _degraded(path: str) -> None:
    metrics.agent_cost_governance_degraded_total.labels(path=path).inc()


async def record_call(
    ctx: SecurityCtx | None,
    *,
    thread_id: str,
    message_id: str | None,
    kind: str,
    model_alias: str,
    priced: PricedCall,
) -> None:
    """Best-effort: writes one event for one model call; never raises.

    Does nothing without a valid ctx (an unattributable call has no tenant to meter to, the same
    rule as every other write on the turn path). A call that reported no usage is counted and skipped."""
    if not valid_ctx(ctx):
        return
    if kind not in KINDS:
        # A programming error, not a runtime condition: loud in tests, and the CHECK constraint
        # would refuse the row anyway. Counted so a typo in production is not silent.
        logger.error("usage_event_unknown_kind", extra={"kind": kind})
        _degraded("usage_event_write")
        return
    if not priced.spent_tokens:
        _degraded("usage_missing")
        return
    if not message_id:
        _degraded("usage_event_identity")
        message_id = uuid.uuid4().hex
    row: UsageEventRow = {
        "event_id": event_id_for(ctx["tenant"], message_id),
        "tenant": ctx["tenant"],
        "principal": ctx["principal"],
        "thread_id": thread_id,
        "kind": kind,
        "model_alias": model_alias,
        "resolved_model": await model_ports.model_catalog.get().resolve(model_alias),
        "input_tokens": priced.input_tokens,
        "output_tokens": priced.output_tokens,
        "cached_input_tokens": priced.cached_input_tokens,
        "total_tokens": priced.total_tokens,
        "cost_usd": priced.cost_usd,
        "price_input_per_token": priced.price.input_per_token if priced.price else None,
        "price_output_per_token": priced.price.output_per_token if priced.price else None,
    }
    global _warned_missing_table
    try:
        await ports.usage_events.get().insert(row)
    except UsageTableMissing:
        # The table (19) is not applied.
        _degraded("usage_event_table_missing")
        if not _warned_missing_table:
            _warned_missing_table = True
            logger.warning(
                "usage_events table is missing; no model call is being metered until postgres-init/19-usage-events.sql is applied"
            )
    except Exception as exc:  # noqa: BLE001 - a failing write must not fail the turn it records; counted and alerted instead
        _degraded("usage_event_write")
        logger.warning("usage_event_write_failed", extra={"error_class": type(exc).__name__})


def reset_state() -> None:
    """Test hook: forget that the missing-table warning was already given."""
    global _warned_missing_table
    _warned_missing_table = False
