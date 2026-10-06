"""The spend allowance: may this caller start another turn? (spec 008, GRAPH_PATTERNS.md
patterns 26 and 35.)

Distinct from `MAX_COST_USD_PER_TURN` (graph_routing.py::should_continue), which only tracks
one turn's own running total: this is the cross-turn accumulator, checked before a turn starts
so an over-budget caller is refused before it reaches the LLM/tool loop.

## Limits

A limit is a (scope, window, dollars) triple. Two scopes — `tenant` (everything the
organisation spends) and `principal` (one person's spend inside it) — and two windows:

  * `day`   — a ROLLING 24h (now - 24h, not a calendar day, so a caller's near-limit status
              never resets mid-day);
  * `month` — the CALENDAR month in UTC, which resets on the 1st like an invoice does.

Only the tenant daily limit is always on. The other three are off at 0 and enabled by a
setting, so a deployment that sets none of them behaves exactly as it did before they existed
and pays for none of their reads. The caller is refused by the FIRST exceeded limit in the
order given (`configured_limits` puts tenant before person: an organisation-wide stop is the
more important thing to tell someone), and only that one is counted.

## Why in-flight holds are added to the ledger sum (tenant scopes)

`spent` (usage_ledger's persisted sum) only reflects turns that have already COMPLETED and
recorded their cost. A sibling turn for the same tenant that is already running has not landed
its row yet, so without counting it N concurrent turns would all read the same stale `spent`,
all pass, and all proceed — a check-then-act race. Every turn reserves `MAX_COST_USD_PER_TURN`
for its duration (`usage_ledger.reserve_budget`), so the tenant limits see a burst the ledger
alone would miss.

Holds are per TENANT, not per person, so a person's limit does not see their own concurrent
turns: one person running several conversations at once can overshoot their personal limit by
up to (concurrent turns) x MAX_COST_USD_PER_TURN. That is a soft guard rail, not an exact
meter, and closing it would mean a schema change to the hold table for a cap whose overshoot is
this small; the tenant limits above it stay exact.

## When the check cannot answer

  * the ledger READ fails — governed by `BUDGET_CHECK_FAILURE_POLICY`. "open" (the default)
    serves the turn and counts the failure (`agent_cost_governance_degraded_total`, alert
    `TenantAllowanceUnenforced`): a ledger outage must not also take down every turn. "closed"
    refuses it with `ErrorCode.BUDGET_CHECK_UNAVAILABLE`: with real money behind the ceiling,
    some deployments would rather refuse than run unmetered. Either way it is a decision.
  * the in-flight hold read fails — always open, to 0.0, counted. It only closes a race between
    concurrent turns; the ledger check beneath it is still enforced.

Everything here takes its limits as arguments, so the decision is a plain function of
(ctx, limits, ledger, holds); runtime.py's thin wrappers read the configured values at call
time, which is what lets tests re-point them.
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.agent import usage_ledger
from app.core import metrics
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

Scope = Literal["tenant", "principal"]
Window = Literal["day", "month"]

DAILY_WINDOW = timedelta(hours=24)

# Fractions of a limit at which a turn that is still ALLOWED is counted and logged — an early
# signal before refusals begin. Only the highest one a turn has crossed is counted, so a rate of
# `agent_budget_threshold_total{threshold="95"}` is "turns served within 5% of a cap".
WARNING_THRESHOLDS = (0.70, 0.85, 0.95)

_WINDOW_WORD = {"day": "daily", "month": "monthly"}

# Threshold crossings already logged by this process, keyed by who/what/threshold and mapped to
# the day or month they were logged in. A tenant that sits at 90% all afternoon would otherwise
# log on every turn it runs; the counter still counts every turn, this only de-noises the log.
# Cleared outright when it grows past _LOGGED_MAX so a long-lived worker cannot leak memory.
_logged_crossings: dict[tuple, str] = {}
_LOGGED_MAX = 10_000


def reset_logged_crossings() -> None:
    """Forgets which threshold crossings were already logged. For tests: process-wide state."""
    _logged_crossings.clear()


@dataclass(frozen=True)
class BudgetLimit:
    scope: Scope
    window: Window
    limit_usd: float


@dataclass(frozen=True)
class Allowance:
    """The answer to "may this turn start?".

    `status` is "ok", "exceeded" (a limit has been used up), or "unavailable" (the check could
    not be made AND the policy is "closed"). For "exceeded" the scope/window/figures are those of
    the limit that refused; for "ok" they are those of the limit closest to its cap. `degraded`
    marks an "ok" granted only because a read failed under the "open" policy, so a caller or a
    test can tell a verified pass from an unverified one."""

    status: Literal["ok", "exceeded", "unavailable"]
    scope: Scope = "tenant"
    window: Window = "day"
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    limit_usd: float = 0.0
    resets_at: datetime | None = None
    degraded: bool = False

    @property
    def refused(self) -> bool:
        return self.status != "ok"


def window_start(window: Window, now: datetime) -> datetime:
    """The earliest instant of `now`'s `window`. `now` must be timezone-aware UTC."""
    if window == "day":
        return now - DAILY_WINDOW
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def window_resets_at(window: Window, now: datetime) -> datetime | None:
    """When a spent window next starts from zero: the 1st of next month for `month`; None for
    the rolling `day`, which has no single reset instant (spend ages out hour by hour)."""
    if window == "day":
        return None
    first = window_start("month", now)
    if first.month == 12:
        return first.replace(year=first.year + 1, month=1)
    return first.replace(month=first.month + 1)


def configured_limits(
    *,
    tenant_day: float,
    tenant_month: float = 0.0,
    principal_day: float = 0.0,
    principal_month: float = 0.0,
) -> list[BudgetLimit]:
    """The limits to enforce, tenant before person. The tenant daily limit is always present
    (a value of 0 there refuses everything, as it always has); the others are present only when
    above 0."""
    limits = [BudgetLimit("tenant", "day", tenant_day)]
    for scope, window, value in (
        ("tenant", "month", tenant_month),
        ("principal", "day", principal_day),
        ("principal", "month", principal_month),
    ):
        if value > 0:
            limits.append(BudgetLimit(scope, window, value))  # type: ignore[arg-type]  # scope/window are the Literal values above
    return limits


async def _spend(limit: BudgetLimit, ctx: SecurityCtx, now: datetime) -> tuple[float, float]:
    """(ledger spend in the limit's window, in-flight holds) for this limit's scope."""
    principal = ctx["principal"] if limit.scope == "principal" else None
    summary = await usage_ledger.usage_summary(
        ctx["tenant"], principal=principal, since=window_start(limit.window, now)
    )
    reserved = await usage_ledger.in_flight_reservation(ctx["tenant"]) if limit.scope == "tenant" else 0.0
    return summary["total_cost_usd"], reserved


def _highest_threshold(fraction: float) -> float | None:
    crossed = [t for t in WARNING_THRESHOLDS if fraction >= t]
    return crossed[-1] if crossed else None


def _note_threshold(ctx: SecurityCtx, limit: BudgetLimit, threshold: float, spent: float, reserved: float, now: datetime) -> None:
    label = f"{round(threshold * 100)}"
    metrics.agent_budget_threshold_total.labels(
        scope=limit.scope, window=limit.window, threshold=label
    ).inc()
    bucket = now.strftime("%Y-%m") if limit.window == "month" else now.strftime("%Y-%m-%d")
    key = (ctx["tenant"], ctx["principal"] if limit.scope == "principal" else "", limit.scope, limit.window, label)
    if _logged_crossings.get(key) == bucket:
        return
    if len(_logged_crossings) >= _LOGGED_MAX:
        _logged_crossings.clear()
    _logged_crossings[key] = bucket
    logger.warning(
        "budget_threshold_crossed",
        extra={
            "tenant": ctx["tenant"],
            "principal": ctx["principal"],
            "scope": limit.scope,
            "window": limit.window,
            "threshold_pct": label,
            "spent_usd": spent,
            "reserved_usd": reserved,
            "limit_usd": limit.limit_usd,
        },
    )


async def check_allowance(
    ctx: SecurityCtx | None,
    *,
    limits: Sequence[BudgetLimit],
    fail_policy: str,
    now: datetime | None = None,
) -> Allowance:
    """Spend over each limit's window (`usage_ledger`) plus, for tenant limits, in-flight holds,
    against that limit.

    An invalid ctx is unattributable, so there is nothing to meter: "ok", without a ledger read.
    The first limit at or past its cap refuses the turn ("exceeded"): counted under its scope and
    window, and logged with the tenant and principal, which the counter deliberately has no label
    for. Limits that still allow the turn but have crossed a warning threshold are counted and
    logged as an early signal. Reads are sequential and only made for limits that are enabled, so
    a deployment with just the daily tenant limit pays for exactly one ledger read, as before.
    """
    if not valid_ctx(ctx):
        return Allowance("ok")
    now = now or datetime.now(UTC)

    spends: list[tuple[float, float]] = []
    try:
        for limit in limits:
            spends.append(await _spend(limit, ctx, now))
    except Exception as exc:  # noqa: BLE001 - a ledger read failing must not by itself take every turn down; BUDGET_CHECK_FAILURE_POLICY decides, and it is counted and alerted either way
        # While this is firing under "open" the allowance is UNENFORCED for every turn that
        # hits it (alert TenantAllowanceUnenforced, spec 008 A1).
        metrics.agent_cost_governance_degraded_total.labels(path="ledger_read").inc()
        logger.warning(
            "tenant_budget_check_failed",
            extra={"error_class": type(exc).__name__, "fail_policy": fail_policy},
        )
        if fail_policy == "closed":
            return Allowance("unavailable")
        return Allowance("ok", degraded=True)

    for limit, (spent, reserved) in zip(limits, spends, strict=True):
        if spent + reserved >= limit.limit_usd:
            metrics.agent_budget_exceeded_total.labels(scope=limit.scope, window=limit.window).inc()
            logger.warning(
                "budget_exceeded",
                extra={
                    "tenant": ctx["tenant"],
                    "principal": ctx["principal"],
                    "scope": limit.scope,
                    "window": limit.window,
                    "spent_usd": spent,
                    "reserved_usd": reserved,
                    "limit_usd": limit.limit_usd,
                },
            )
            return Allowance(
                "exceeded", limit.scope, limit.window, spent, reserved, limit.limit_usd,
                window_resets_at(limit.window, now),
            )

    closest: Allowance | None = None
    closest_fraction = -1.0
    for limit, (spent, reserved) in zip(limits, spends, strict=True):
        fraction = (spent + reserved) / limit.limit_usd if limit.limit_usd > 0 else 0.0
        threshold = _highest_threshold(fraction)
        if threshold is not None:
            _note_threshold(ctx, limit, threshold, spent, reserved, now)
        if fraction > closest_fraction:
            closest_fraction = fraction
            closest = Allowance(
                "ok", limit.scope, limit.window, spent, reserved, limit.limit_usd,
                window_resets_at(limit.window, now),
            )
    return closest or Allowance("ok")


def refusal_envelope(allowance: Allowance) -> ErrorEnvelope:
    """The caller-facing error for a refused `allowance`. Never call it for an "ok" one.

    A person who hit their OWN limit gets `personal_budget_exceeded` and is told it is theirs;
    an organisation-wide stop keeps the long-standing `tenant_budget_exceeded` code, so existing
    clients branching on it are unaffected. `details` names the scope and window, and the
    instant a monthly window resets."""
    if allowance.status == "unavailable":
        return ErrorEnvelope(
            code=ErrorCode.BUDGET_CHECK_UNAVAILABLE,
            message="Usage could not be verified right now, so this request was not started. Please try again shortly.",
        )
    word = _WINDOW_WORD[allowance.window]
    details: dict = {"scope": allowance.scope, "window": allowance.window}
    if allowance.resets_at is not None:
        details["resets_at"] = allowance.resets_at.isoformat()
    if allowance.scope == "principal":
        return ErrorEnvelope(
            code=ErrorCode.PERSONAL_BUDGET_EXCEEDED,
            message=f"You have reached your personal {word} usage budget. Please try again later.",
            details=details,
        )
    return ErrorEnvelope(
        code=ErrorCode.TENANT_BUDGET_EXCEEDED,
        message=f"This tenant's {word} usage budget has been reached. Please try again later.",
        details=details,
    )
