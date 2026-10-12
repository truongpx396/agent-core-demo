"""One immutable usage event per model call (postgres-init/19-usage-events.sql).

`usage_ledger` records a completed TURN, summed, with no key that names a call. That is enough for
a spend cap and not enough for billing: a billing meter must be able to say "this exact call, once",
so that a replayed write, a retried export and a provider that deduplicates by a caller-supplied id
(Stripe `identifier`, Polar `external_id`) all agree on what one call is. This module is that meter, and
since specs/010 T030 it is also what the dollar caps and `GET /usage` sum (`app/agent/spend.py`).

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

Fail OPEN, counted, alerted. A failed write must not fail the turn it records, but a lost event is lost revenue and nobody is told by a log line, so:
  * `agent_cost_governance_degraded_total{path="usage_event_write"}`  -> UsageEventWriteFailing (critical)
  * `...{path="usage_event_table_missing"}` (migration 19, or 21 once CREDITS_PER_USD is set, not applied)
    -> UsageEventTableMissing (warning)
  * `...{path="credit_debit"}`: the event was written and its wallet debit was not -> CreditDebitFailing (critical)
  * `...{path="export_enqueue"}`: the event was written and queuing it for export was not -> UsageExportEnqueueFailing (critical)
  * `...{path="usage_event_identity"}`: a response with no message id, so it got a random one
    and cannot be de-duplicated (a replay would double-count it);
  * `...{path="usage_missing"}`: the provider reported no usage for a call, so nothing could be
    metered. Counted, not alerted: a local model may do this.
There is no kill switch: the caps sum these rows, so switching the writes off would make every cap read $0
(`USAGE_EVENTS_ENABLED=false` is refused at startup, `config.py`). Because the caps now depend on the write,
a failing one is also a cap under-counting: `UsageEventWriteFailing` is the alert for both.

## Charging credits (specs/010 T015)

With `CREDITS_PER_USD` set, an event is also RATED: `credits = round_half_up(cost_usd x CREDITS_PER_USD x
MARKUP, 6)`, stored on the row together with the rate and markup used (postgres-init/21), so the figure is
reproducible from the row and a later rate change never rewrites a past call. The cost is first rounded
to the column's twelve places and that rounded figure is what is stored AND what is multiplied, so
`credits == credits_for_cost(cost_usd, credits_per_usd, markup)` holds for the row exactly. An UNPRICED call
has `credits` NULL and debits nothing (unknown is never silently free; it is already counted once by
`pricing.note_unpriced`).

A tenant WITH a wallet is then debited, in the SAME transaction as the insert and keyed by the event id, so
a replayed call that finds its event already there debits nothing (the insert reports it, and the debit
key would refuse it anyway). A tenant with no wallet costs one indexed lookup (`credits.debit_in`).

**A wallet fault must not lose the meter, so the debit runs in a savepoint.** If it raises, only the debit
rolls back: the event is still committed (with the credits it was worth, so the missing charge is
visible and replayable: the debit key IS the event id and the row holds every figure it needs), the
failure is counted as `agent_cost_governance_degraded_total{path="credit_debit"}` and pages
(`CreditDebitFailing`). The alternative, one all-or-nothing transaction, would turn a wallet outage into a
meter outage, and a lost event cannot be repaired while an uncharged one can. The residual: until a repair
job exists (specs/010 PR 6's reconciliation names the gap), an uncharged event stays uncharged.

With `CREDITS_PER_USD` unset the row has no credit keys at all and the original statement is used, so a
deployment that has not applied postgres-init/21 sees no change.

## Queuing for export (specs/010 T023)

For a tenant linked to a provider that bills on usage (it declares USAGE_EXPORT) and that this process has enabled
(`BILLING_PROVIDERS`), the same transaction also inserts a `usage_export_outbox` row, so the event and its pending export
cannot disagree about what exists, and a replay that finds the event already there queues nothing. It uses a savepoint like the
charge, for the same reason: the meter outranks everything built on it, so a failure to queue (counted as
`{path="export_enqueue"}`, alert UsageExportEnqueueFailing) loses the export of one event, never the event. With no such provider
enabled (the default) nothing extra is sent: one list lookup and no statement.

## There is no second record any more

For a while (specs/010 PR 1a to T030c1) a turn was ALSO summed into the per-turn `usage_ledger`: the caps read it, and then,
after the caps moved here (T030b), it stayed as the way back and as a second record for the reconciliation. Both
are gone (T030c): nothing writes the ledger, the reconciliation compares the events with the gateway's own spend log,
and a failing write here is the one thing that makes a cap under-count (`UsageEventWriteFailing`). The old table is frozen history
that `scripts/usage_events_carry_over.py` copied into this one.
"""
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal

from psycopg import AsyncConnection
from psycopg import errors as pg_errors

from app.agent.model_resolver import resolve_model
from app.agent.pricing import PricedCall
from app.agent.sql_store import get_connection
from app.billing import credits, providers
from app.core import metrics
from app.core.config import (
    BILLING_PROVIDERS,
    CREDITS_PER_USD,
    MARKUP,
)
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

# The closed set the table's CHECK enforces. Embeddings and cron arrive with the PRs that route them here.
KINDS = frozenset({"chat", "followups", "compaction", "subagent", "embedding", "cron"})

# `cost_usd` has twelve decimal places in the table; the credits are computed from the cost AS STORED.
_COST_UNIT = Decimal("0.000000000001")

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
    row.update(_rating(priced))
    global _warned_missing_table
    try:
        await _insert(row)
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn):
        # The table (19), or the credit columns CREDITS_PER_USD needs (21), are not applied.
        _degraded("usage_event_table_missing")
        if not _warned_missing_table:
            _warned_missing_table = True
            logger.warning(
                "usage_events table is missing (or lacks the credit columns CREDITS_PER_USD needs); no model call "
                "is being metered until postgres-init/19-usage-events.sql and 21-usage-event-credits.sql are applied"
            )
    except Exception as exc:  # noqa: BLE001 - a failing write must not fail the turn it records; counted and alerted instead
        _degraded("usage_event_write")
        logger.warning("usage_event_write_failed", extra={"error_class": type(exc).__name__})


def _rating(priced: PricedCall) -> dict:
    """The credit fields of an event, or {} when credits are off (no `CREDITS_PER_USD`).

    The keys are present exactly when credits are on: `_insert` picks its statement by them, so a
    deployment that has not set a rate never mentions a column it may not have. `credits` is None for an
    unpriced call (unknown, not free). The cost is the one the row stores (see `_COST_UNIT`)."""
    if CREDITS_PER_USD is None:
        return {}
    rating: dict = {"credits": None, "credits_per_usd": CREDITS_PER_USD, "markup": MARKUP}
    if priced.cost_usd is not None:
        cost = Decimal(str(priced.cost_usd)).quantize(_COST_UNIT, rounding=ROUND_HALF_UP)
        rating["cost_usd"] = cost
        rating["credits"] = credits.credits_for_cost(cost, CREDITS_PER_USD, MARKUP)
    return rating


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
_INSERT_RATED = (
    f"INSERT INTO usage_events ({_COLUMNS}, credits, credits_per_usd, markup) "
    f"VALUES ({_VALUES}, %(credits)s, %(credits_per_usd)s, %(markup)s) ON CONFLICT (event_id) DO NOTHING"
)


async def _insert(row: dict) -> bool:
    """True if a row was written, False if `event_id` was already there. The statement relies on
    the table's PRIMARY KEY for `ON CONFLICT`, which a fake cursor cannot prove: the real-Postgres
    test (tests/integration/test_usage_events_real_postgres.py) does.

    A rated row (credits on) is followed, in the same transaction, by the debit for a tenant with a
    wallet; see "Charging credits" in the module docstring. A duplicate is never charged."""
    rated = "credits" in row
    async with get_connection() as conn:
        cur = await conn.execute(_INSERT_RATED if rated else _INSERT, row)
        inserted = cur.rowcount == 1
        if inserted and rated and row["credits"] is not None:
            await _charge(conn, row)
        if inserted:
            await _enqueue_export(conn, row)
        return inserted


async def _enqueue_export(conn: AsyncConnection, row: dict) -> None:
    """Queues the event for every usage-billing provider this tenant is linked to (and this process has enabled), inside a
    savepoint so a fault undoes only the queuing. Never raises. A tenant with no such link inserts nothing: the SELECT is
    over `billing_customers`, so the outbox only ever holds events someone has a customer to send them under."""
    exporters = providers.usage_export_providers(BILLING_PROVIDERS)
    if not exporters:
        return
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO usage_export_outbox (provider, event_id, tenant) "
                "SELECT c.provider, %(event_id)s, %(tenant)s FROM billing_customers c "
                "WHERE c.tenant = %(tenant)s AND c.provider = ANY(%(providers)s) ON CONFLICT (provider, event_id) DO NOTHING",
                {"event_id": row["event_id"], "tenant": row["tenant"], "providers": list(exporters)},
            )
    except Exception as exc:  # noqa: BLE001 - a failure to queue must not lose the event it concerns; counted and alerted instead (UsageExportEnqueueFailing)
        _degraded("export_enqueue")
        logger.warning("usage_export_enqueue_failed", extra={"error_class": type(exc).__name__})


async def _charge(conn: AsyncConnection, row: dict) -> None:
    """Debits the tenant's wallet for the event just inserted on `conn`, inside a savepoint so that a
    wallet fault undoes only the debit and never the event (module docstring: the meter outranks the
    charge, because a lost event cannot be repaired and an uncharged one can). Never raises."""
    try:
        async with conn.transaction():
            await credits.debit_in(
                conn,
                row["tenant"],
                row["credits"],
                idempotency_key=row["event_id"],
                usage_event_id=row["event_id"],
                reason=f"model call ({row['kind']})",
                pricing=credits.Pricing(
                    cost_usd=row["cost_usd"], credits_per_usd=row["credits_per_usd"], markup=row["markup"]
                ),
            )
    except Exception as exc:  # noqa: BLE001 - a wallet fault must not lose the event it was charging for; counted and alerted instead (CreditDebitFailing)
        _degraded("credit_debit")
        logger.warning("credit_debit_failed", extra={"error_class": type(exc).__name__})


def reset_state() -> None:
    """Test hook: forget that the missing-table warning was already given."""
    global _warned_missing_table
    _warned_missing_table = False
