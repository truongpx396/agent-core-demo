"""One immutable usage event per model call (postgres-init/19-usage-events.sql).

`usage_ledger` recorded a completed TURN, summed, with no key that names a call. That was enough for a first spend cap and not enough for a
meter: a meter must be able to say "this exact call, once", so that a replayed write and anything built on the record all agree on what one call
is. This module is that meter, and since specs/010 T030 it is also what the dollar caps and `GET /usage` sum (`app/agent/spend.py`).

## Identity

`event_id = uuid5(NAMESPACE, f"{tenant}|{message_id}")`, where `message_id` is the response's
`AIMessage.id`. Verified against the installed langchain-core / langchain-openai / langgraph
(specs/010 research R5, pinned by tests/agent/test_usage_events.py): langchain-core assigns it as
`run-<run id>-<index>`, it is unique per invocation (two identical calls that came back with the
SAME provider id still got different ones), and it is identical after a checkpoint round trip. The
provider's own `chatcmpl-...` id is deliberately not used: a backend may return a fixed or missing one.
A LangGraph node retry that calls the model again gets a new id and a new event, which is right: a
second paid call happened.

## Failure policy

Fail OPEN, counted, alerted. A failed write must not fail the turn it records, but a lost event is lost spend that no cap sees and
nobody is told by a log line, so:
  * `agent_cost_governance_degraded_total{path="usage_event_write"}`  -> UsageEventWriteFailing (critical)
  * `...{path="usage_event_table_missing"}` (migration 19 not applied) -> UsageEventTableMissing (warning)
  * `...{path="usage_event_identity"}`: a response with no message id, so it got a random one
    and cannot be de-duplicated (a replay would double-count it);
  * `...{path="usage_missing"}`: the provider reported no usage for a call, so nothing could be
    metered. Counted, not alerted: a local model may do this.
There is no kill switch: the caps sum these rows, so switching the writes off would make every cap read $0
(`USAGE_EVENTS_ENABLED=false` is refused at startup, `config.py`). Because the caps now depend on the write,
a failing one is also a cap under-counting: `UsageEventWriteFailing` is the alert for both.

## Billing is one call, and not this module's business

A model call is also billable usage. After inserting its own row this module hands the billing package ONE plain value (the tenant, the
event id, the kind, the cost, the model and the token count) through `app/billing_integration/port.py`, on the same connection and in the same
transaction, and only when the row was newly inserted, so a replay that finds its event already there hands over nothing. What the package does
with it (rating it in credits, keeping its own record, debiting a wallet, queuing an export) and how it fails (it never raises; every way it can
fail is counted and alerted by the package) is the package's: see `packages/billing/src/credit_billing/usage.py`. This module no longer knows
what is done with the figures, and the table carries no billing column.

## There is no second record any more

For a while (specs/010 PR 1a to T030c1) a turn was ALSO summed into the per-turn `usage_ledger`: the caps read it, and then,
after the caps moved here (T030b), it stayed as the way back and as a second record for the reconciliation. Both
are gone (T030c): nothing writes the ledger, the reconciliation compares the events with the gateway's own spend log,
and a failing write here is the one thing that makes a cap under-count (`UsageEventWriteFailing`). The old table is frozen history
that `scripts/usage_events_carry_over.py` copied into this one.
"""
import logging
import uuid

from psycopg import errors as pg_errors

from app.agent.model_resolver import resolve_model
from app.agent.pricing import PricedCall
from app.agent.sql_store import get_connection
from app.billing_integration import port as billing_port
from app.core import metrics
from app.core.security import SecurityCtx, valid_ctx

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
