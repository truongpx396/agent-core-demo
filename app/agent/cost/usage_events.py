"""One immutable usage event per model call (postgres-init/19-usage-events.sql).

The per-turn `usage_ledger` had no key that named a call; a meter must say "this exact call, once", so a
replayed write and everything built on the record agree on what one call is. This module is that meter, and
the dollar caps and `GET /usage` sum it (`app/agent/cost/spend.py`).

## Identity

`event_id = uuid5(NAMESPACE, f"{tenant}|{message_id}")`, with `message_id` the response's `AIMessage.id`.
Verified against the installed langchain-core / langchain-openai / langgraph (specs/010 research R5, pinned by
tests/agent/cost/test_usage_events.py): langchain-core assigns `run-<run id>-<index>`, unique per invocation
(two identical calls whose provider returned the SAME id still got different ones) and identical after a
checkpoint round trip. The provider's own `chatcmpl-...` id is deliberately not used: a backend may return a
fixed or missing one. A LangGraph node retry that calls the model again gets a new id and a new event, which
is right: a second paid call happened.

## Failure policy

Fail OPEN, counted, alerted: a failed write mustn't fail the turn, but a lost event is lost spend no cap sees
and a log line tells nobody.
  * `agent_cost_governance_degraded_total{path="usage_event_write"}` -> UsageEventWriteFailing (critical)
  * `...{path="usage_event_table_missing"}` (migration 19 not applied) -> UsageEventTableMissing (warning)
  * `...{path="usage_event_identity"}`: a response with no message id got a random one and can't be
    de-duplicated (a replay would double-count it);
  * `...{path="usage_missing"}`: the provider reported no usage, so nothing was metered. Counted, not alerted:
    a local model may do this.
There is no kill switch: the caps sum these rows, so switching writes off would make every cap read $0
(`USAGE_EVENTS_ENABLED=false` is refused at startup, `config.py`). A failing write is also a cap
under-counting; `UsageEventWriteFailing` is the alert for both.

## Billing is one call, and not this module's business

After inserting its row, this module hands the billing package ONE plain value (tenant, event id, kind, cost,
model, token count) through `app/billing_integration/port.py`, on the same connection and transaction, and
only when the row was newly inserted (a replay hands over nothing). What the package does with it and how it
fails (it never raises; every failure is counted and alerted by the package) is its own:
`packages/billing/src/credit_billing/usage.py`. The table carries no billing column.

## No second record

The per-turn `usage_ledger` is gone (specs/010 T030c): nothing writes it, the reconciliation compares the
events with the gateway's spend log, and the old table is frozen history that
`scripts/usage_events_carry_over.py` copied here.
"""
import logging
import uuid

from psycopg import errors as pg_errors

from app.agent.cost.pricing import PricedCall
from app.billing_integration import port as billing_port
from app.core import metrics
from app.core.model_resolver import resolve_model
from app.core.security import SecurityCtx, valid_ctx
from app.core.sql_store import get_connection

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
    row = {
        "event_id": event_id_for(ctx["tenant"], message_id),
        "tenant": ctx["tenant"],
        "principal": ctx["principal"],
        "thread_id": thread_id,
        "kind": kind,
        "model_alias": model_alias,
        "resolved_model": await resolve_model(model_alias),
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
        await _insert(row)
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn):
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


_COLUMNS = (
    "event_id, tenant, principal, thread_id, kind, model_alias, resolved_model, "
    "input_tokens, output_tokens, cached_input_tokens, total_tokens, cost_usd, "
    "price_input_per_token, price_output_per_token"
)
_VALUES = (
    "%(event_id)s, %(tenant)s, %(principal)s, %(thread_id)s, %(kind)s, "
    "%(model_alias)s, %(resolved_model)s, %(input_tokens)s, %(output_tokens)s, "
    "%(cached_input_tokens)s, %(total_tokens)s, %(cost_usd)s, "
    "%(price_input_per_token)s, %(price_output_per_token)s"
)
_INSERT = f"INSERT INTO usage_events ({_COLUMNS}) VALUES ({_VALUES}) ON CONFLICT (event_id) DO NOTHING"


async def _insert(row: dict) -> bool:
    """True if a row was written, False if `event_id` was already there. The statement relies on
    the table's PRIMARY KEY for `ON CONFLICT`, which a fake cursor cannot prove: the real-Postgres
    test (tests/integration/test_usage_events_real_postgres.py) does.

    A newly written row is handed to billing in the same transaction ("Billing is one call" in the module docstring); a duplicate is not."""
    async with get_connection() as conn:
        cur = await conn.execute(_INSERT, row)
        inserted = cur.rowcount == 1
        if inserted:
            await billing_port.record_usage_in(
                conn,
                billing_port.Usage(
                    tenant=row["tenant"], event_id=row["event_id"], kind=row["kind"], cost_usd=row["cost_usd"],
                    model=row["resolved_model"] or row["model_alias"], tokens=row["total_tokens"],
                ),
            )
        return inserted


def reset_state() -> None:
    """Test hook: forget that the missing-table warning was already given."""
    global _warned_missing_table
    _warned_missing_table = False
