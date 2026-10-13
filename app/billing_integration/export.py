"""Money out: usage events reach a usage-billing provider exactly once, or fail loudly
(specs/010-credit-billing-readiness, T023-T025; postgres-init/23-usage-export-outbox.sql).

## The shape

A tenant linked to a provider that bills on usage (it declares `USAGE_EXPORT`) gets one `usage_export_outbox` row per
usage event, written in the event's own transaction (`usage_events._insert`). This module drains that table. It is the
only code that sends usage to a provider, and it is built for the three facts research R1 established:

  1. Providers deduplicate by a caller-supplied id (Stripe `identifier`, Polar `external_id`), so the usage event's
     deterministic id is the provider's idempotency key and a retry is always safe.
  2. Stripe accepts an event only if its timestamp is within the past 35 days, so retrying must give up BEFORE that
     (`BILLING_EXPORT_MAX_AGE_DAYS`, at most 34) and say so loudly: an event that is retried past the window is not
     delayed, it is discarded by the provider without anyone being told.
  3. A provider can be down for hours, so every loop here has a ceiling (an attempt budget, a backoff cap, a call
     deadline, a batch size, an age) and a metric.

## The duplicate story (constitution IV)

| Duplicate arrives as | Caught by |
|---|---|
| a replayed usage event queued twice | `PRIMARY KEY (provider, event_id)` + `ON CONFLICT DO NOTHING`, in the event's transaction |
| two workers claiming the same row | `FOR UPDATE SKIP LOCKED`: each takes a disjoint batch |
| a crash between "sent" and "marked sent" | the provider's own idempotency key (the event id): the resend is reported as a duplicate, which is success. **This is the one window the database cannot see.** |
| a call that timed out but landed | the same: it is retried with the same id |

## What a worker does, per provider, per pass

Claim the due batch (`pending`, `next_attempt_at <= now()`, oldest first) with `FOR UPDATE SKIP LOCKED`, send it to the
adapter one customer at a time under a deadline, and record every row's verdict, all in ONE transaction: if the process
dies mid-way the rows are simply pending again. The locks are held while the provider is called, which is exactly why that call
has a deadline (`BILLING_EXPORT_CALL_TIMEOUT_SECONDS`).

## Verdicts (`verdicts`, pure, so each rule has its own test)

accepted or duplicate -> `sent`. A retryable failure -> back off (`min(base * 2**(k-1), cap)` after the k-th failure), and
`failed` once the attempt budget is spent. A permanent failure -> `failed` at once. An id the adapter did not mention, or an
adapter that raised or timed out -> retryable (nothing is known to have landed, and a resend is safe). A customer with no
link left to send under -> `failed`, not retried for 30 days to no purpose. `expired` is decided by age, before sending.

## Gaps this leaves, disclosed

  * The worker's own liveness is not alerted: a dead worker shows only as `UsageExportStuck` once the oldest event is old.
  * An event is queued only by a process that has the provider enabled (`BILLING_PROVIDERS`); one written elsewhere is
    never queued, and nothing back-fills it yet (the reconciliation, PR 6, compares events with the gateway and the
    ledger, and is where this would be found).
  * The adapters are written later, so the call deadline and the response mapping are proven against the `fake` only.
"""
import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.agent.sql_store import get_connection
from app.billing.providers.base import (
    BillingCustomer,
    BillingProvider,
    ExportResult,
    UsageEvent,
)
from app.core import metrics
from app.core.config import (
    BILLING_EXPORT_BACKOFF_BASE_SECONDS,
    BILLING_EXPORT_BACKOFF_CAP_SECONDS,
    BILLING_EXPORT_BATCH_SIZE,
    BILLING_EXPORT_CALL_TIMEOUT_SECONDS,
    BILLING_EXPORT_MAX_AGE_DAYS,
    BILLING_EXPORT_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)

Status = Literal["sent", "pending", "failed"]


@dataclass(frozen=True)
class Due:
    """One claimed outbox row with what the provider needs to be told about it."""

    event_id: str
    tenant: str
    attempts: int  # attempts already made, before this one
    customer_ref: str | None  # None: the tenant's link to this provider is gone
    usage: UsageEvent


@dataclass(frozen=True)
class Verdict:
    status: Status
    delay_seconds: int = 0  # for `pending`: how long before the next attempt
    error: str | None = None


def backoff_seconds(failures: int, base: int, cap: int) -> int:
    """The wait after the `failures`-th failed attempt: base, 2*base, 4*base, ... never above `cap`. The exponent is
    bounded before it is used, so a large attempt count cannot build an enormous integer on the way to being capped."""
    return min(base * (2 ** min(max(failures, 1) - 1, 30)), cap)


def verdicts(
    batch: Sequence[Due],
    result: ExportResult | None,
    error: BaseException | None,
    *,
    max_attempts: int,
    base: int,
    cap: int,
) -> dict[str, Verdict]:
    """The outcome of every row in `batch` (one customer's events, sent in one call), as a pure function of what the
    adapter said. `result` is None when it raised or timed out, in which case `error` says what."""

    def retry(due: Due, reason: str) -> Verdict:
        failures = due.attempts + 1
        if failures >= max_attempts:
            return Verdict("failed", error=f"attempts_exhausted:{reason}"[:100])
        return Verdict("pending", delay_seconds=backoff_seconds(failures, base, cap), error=reason[:100])

    if result is None:
        reason = type(error).__name__ if error is not None else "no_result"  # a class name: an exception's text can carry a URL or a key
        return {due.event_id: retry(due, reason) for due in batch}

    failed = {f.event_id: f for f in result.failed}
    landed = set(result.accepted) | set(result.duplicate)
    out: dict[str, Verdict] = {}
    for due in batch:
        if due.event_id in failed:
            # A provider that says both "accepted" and "failed" for one id is ambiguous; the safe reading is that it
            # did not land, and a resend is harmless because the provider dedupes by the id.
            out[due.event_id] = retry(due, "provider_retryable") if failed[due.event_id].retryable else Verdict("failed", error="provider_permanent")
        elif due.event_id in landed:
            out[due.event_id] = Verdict("sent")
        else:
            out[due.event_id] = retry(due, "unreported")
    return out


async def run_once(
    providers: Mapping[str, BillingProvider],
    *,
    batch_size: int = BILLING_EXPORT_BATCH_SIZE,
    max_attempts: int = BILLING_EXPORT_MAX_ATTEMPTS,
    base: int = BILLING_EXPORT_BACKOFF_BASE_SECONDS,
    cap: int = BILLING_EXPORT_BACKOFF_CAP_SECONDS,
    max_age_days: int = BILLING_EXPORT_MAX_AGE_DAYS,
    call_timeout: float = BILLING_EXPORT_CALL_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """One bounded pass over every provider in `providers`. Returns how many events ended each way
    (`sent`, `retry`, `failed`, `expired`, summed over providers). Never raises for a provider's misbehaviour: a failing
    provider is a verdict, not a crash, so one bad provider cannot stop the others."""
    totals: dict[str, int] = defaultdict(int)
    for name, count in (await _expire(max_age_days, list(providers))).items():
        totals["expired"] += count
        metrics.agent_usage_export_total.labels(provider=name, outcome="expired").inc(count)
        logger.error("usage_export_expired", extra={"provider": name, "events": count, "max_age_days": max_age_days})
    for name, adapter in providers.items():
        for outcome, count in (await _drain(name, adapter, batch_size, max_attempts, base, cap, call_timeout)).items():
            totals[outcome] += count
            metrics.agent_usage_export_total.labels(provider=name, outcome=outcome).inc(count)
        await _set_age_gauge(name)
    return dict(totals)


async def _expire(max_age_days: int, names: Sequence[str]) -> dict[str, int]:
    """Gives up, loudly, on pending events of the providers this worker SERVES that are older than the age limit (see module
    docstring, fact 2). Rows another worker is sending right now are skipped, not waited for. Scoped to `names` because a
    worker acts only for the providers it is configured for: it holds their adapters, counts under their labels, and sets
    their gauge. (Disclosed: events of a provider that is no longer enabled are not touched, and nothing alerts on them.)"""
    async with get_connection() as conn:
        cur = await conn.execute(
            "WITH due AS ("
            "SELECT o.provider, o.event_id FROM usage_export_outbox o JOIN usage_events e ON e.event_id = o.event_id AND e.tenant = o.tenant "
            "WHERE o.status = 'pending' AND o.provider = ANY(%s) AND e.occurred_at < now() - make_interval(days => %s) "
            "FOR UPDATE OF o SKIP LOCKED) "
            "UPDATE usage_export_outbox o SET status = 'expired', last_error_class = 'max_age' "
            "FROM due WHERE o.provider = due.provider AND o.event_id = due.event_id RETURNING o.provider",
            (list(names), max_age_days),
        )
        counts: dict[str, int] = defaultdict(int)
        for (provider,) in await cur.fetchall():
            counts[provider] += 1
        return dict(counts)


async def _drain(
    name: str, adapter: BillingProvider, batch_size: int, max_attempts: int, base: int, cap: int, call_timeout: float
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    async with get_connection() as conn:  # ONE transaction: claim, send, record. A crash anywhere leaves the rows pending.
        cur = await conn.execute(
            "SELECT o.event_id, o.tenant, o.attempts, e.occurred_at, e.credits, e.cost_usd, COALESCE(e.resolved_model, e.model_alias), "
            "e.total_tokens, c.customer_ref "
            "FROM usage_export_outbox o JOIN usage_events e ON e.event_id = o.event_id AND e.tenant = o.tenant "
            "LEFT JOIN billing_customers c ON c.tenant = o.tenant AND c.provider = o.provider "
            "WHERE o.provider = %s AND o.status = 'pending' AND o.next_attempt_at <= now() "
            "ORDER BY o.next_attempt_at, o.created_at LIMIT %s FOR UPDATE OF o SKIP LOCKED",
            (name, batch_size),
        )
        groups: dict[tuple[str, str | None], list[Due]] = defaultdict(list)
        for event_id, tenant, attempts, occurred_at, credits, cost_usd, model, tokens, customer_ref in await cur.fetchall():
            usage = UsageEvent(event_id, occurred_at, _decimal(credits), _decimal(cost_usd), model, tokens)
            groups[(tenant, customer_ref)].append(Due(event_id, tenant, attempts, customer_ref, usage))

        decided: dict[str, Verdict] = {}
        for (tenant, customer_ref), batch in groups.items():
            if customer_ref is None:
                decided.update({due.event_id: Verdict("failed", error="unlinked_customer") for due in batch})
                continue
            result, error = await _send(adapter, BillingCustomer(tenant, name, customer_ref), batch, call_timeout)
            decided.update(verdicts(batch, result, error, max_attempts=max_attempts, base=base, cap=cap))

        for event_id, verdict in decided.items():
            await _record(conn, name, event_id, verdict)
            counts["retry" if verdict.status == "pending" else verdict.status] += 1
    return dict(counts)


async def _send(
    adapter: BillingProvider, customer: BillingCustomer, batch: Sequence[Due], call_timeout: float
) -> tuple[ExportResult | None, BaseException | None]:
    try:
        return await asyncio.wait_for(adapter.export_usage(customer, [due.usage for due in batch]), timeout=call_timeout), None
    except Exception as exc:  # noqa: BLE001 - a provider's failure (or a deadline) is a verdict on its rows, never a crash of the pass; counted as `retry`/`failed` and alerted
        logger.warning(
            "usage_export_call_failed",
            extra={"provider": adapter.name, "tenant": customer.tenant, "events": len(batch), "error_class": type(exc).__name__},
        )
        return None, exc


async def _record(conn, provider: str, event_id: str, verdict: Verdict) -> None:
    if verdict.status == "sent":
        sql = "UPDATE usage_export_outbox SET status = 'sent', attempts = attempts + 1, sent_at = now(), last_error_class = NULL WHERE provider = %s AND event_id = %s"
        params: tuple = (provider, event_id)
    elif verdict.status == "failed":
        sql = "UPDATE usage_export_outbox SET status = 'failed', attempts = attempts + 1, last_error_class = %s WHERE provider = %s AND event_id = %s"
        params = (verdict.error, provider, event_id)
    else:
        sql = (
            "UPDATE usage_export_outbox SET attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => %s), "
            "last_error_class = %s WHERE provider = %s AND event_id = %s"
        )
        params = (verdict.delay_seconds, verdict.error, provider, event_id)
    await conn.execute(sql, params)


async def _set_age_gauge(provider: str) -> None:
    """How old the oldest still-pending event is, so UsageExportStuck can say so while there is still time to send it."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(EXTRACT(EPOCH FROM now() - MIN(created_at)), 0) FROM usage_export_outbox WHERE provider = %s AND status = 'pending'",
            (provider,),
        )
        (age,) = await cur.fetchone()
    metrics.agent_usage_export_oldest_pending_age_seconds.labels(provider=provider).set(float(age))


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(value)
