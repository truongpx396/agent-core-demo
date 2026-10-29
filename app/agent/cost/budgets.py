"""The spend allowance: may this caller start another turn? (spec 008, GRAPH_PATTERNS.md 26 and 35.)

The cross-turn counterpart of `MAX_COST_USD_PER_TURN`, checked before a turn starts so an over-budget caller
is refused before reaching the LLM/tool loop.

## Limits

A limit is a (scope, window, dollars) triple. Scopes: `tenant` and `principal` (one person). Windows: `day`
(rolling 24h) and `month` (calendar month, UTC).

Only the tenant daily limit is always on; the other three are off at 0, so an unconfigured deployment pays for
none of their reads. The first exceeded limit refuses and only it is counted (tenant is checked before
person).

## Overrides

The four limits are Settings defaults. `budget_policies` (operator-edited,
postgres-init/18-budget-policies.sql) overrides them per tenant, per person, or for every person (`*`):
`resolve_limits` applies person, else `*`, else default. NULL means no cap; 0 refuses everything (the suspend
switch). The override read fails under the same policy as the spend read.

## Spend, and why holds are added (tenant scopes)

Usage events land only when a call returns, so N concurrent turns would all read a stale `spent` and all pass
(check-then-act). Every turn therefore reserves `MAX_COST_USD_PER_TURN` (`budget_holds.reserve_budget`). A
running turn is counted twice (hold plus spend so far): a conservative over-count that can refuse a sibling a
little early and never lets a burst through.

Holds are per tenant, so a person's limit can be overshot by up to (concurrent turns) x
`MAX_COST_USD_PER_TURN`; closing that needs a schema change for a small overshoot. Tenant limits stay exact.

## The credit gate (specs/010 T016, the billing package's since specs/013)

A prepaid balance that only a grant refills. This module asks one question through the port,
`billing_port.admit`, and maps the answer: `exhausted` -> `insufficient_credits`, `unavailable` ->
`BUDGET_CHECK_UNAVAILABLE` (blaming no one's balance), `degraded` -> served, marked `degraded`.

- Holds are a callable awaited only for a tenant with a wallet; the gate converts them to credits.
- It runs after the dollar limits, so a spend cap is what a caller is told about when both apply.
- With enforcement off it reads nothing. The hold read stays open (0.0) on its own failure.

## When the check cannot answer

- The spend read fails: `BUDGET_CHECK_FAILURE_POLICY`. "open" (default) serves the turn and counts it
  (`agent_cost_governance_degraded_total{path="ledger_read"}`, alert `TenantAllowanceUnenforced`); "closed"
  refuses with `BUDGET_CHECK_UNAVAILABLE`.
- The hold read fails: always open (0.0) and counted; the spend check beneath it still applies.

The decision is a plain function of (ctx, limits, spend, holds); the wrappers in app/agent/runtime/core.py
read configured values at call time, which lets tests re-point them.
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.agent.cost import budget_holds, budget_policies, spend
from app.billing_integration import port as billing_port
from app.core import metrics
from app.core.errors import ErrorCode, ErrorEnvelope
from app.core.security import SecurityCtx, valid_ctx
from app.ports.cost import ALL_PRINCIPALS, TENANT_SUBJECT, Override

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

    `status` is "ok", "exceeded" (a limit has been used up), "insufficient_credits" (the tenant's
    wallet has nothing left to spend), or "unavailable" (the check could not be made AND the policy
    is "closed"). For "exceeded" the scope/window/figures are those of
    the limit that refused; for "ok" they are those of the limit closest to its cap. `degraded`
    marks an "ok" granted only because a read failed under the "open" policy, so a caller or a
    test can tell a verified pass from an unverified one."""

    status: Literal["ok", "exceeded", "unavailable", "insufficient_credits"]
    scope: Scope = "tenant"
    window: Window = "day"
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    limit_usd: float = 0.0
    resets_at: datetime | None = None
    degraded: bool = False
    # Set only when the credit gate refused ("insufficient_credits"): the wallet's available credits and
    # the in-flight holds, in credits. The scope/window/usd figures above mean nothing for that status.
    available_credits: Decimal | None = None
    reserved_credits: Decimal | None = None

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


@dataclass(frozen=True)
class Defaults:
    """The Settings values a limit falls back to when no override applies. 0 means off for
    every field but `tenant_day`, which is always enforced (0 there refuses everything)."""

    tenant_day: float
    tenant_month: float = 0.0
    principal_day: float = 0.0
    principal_month: float = 0.0


def resolve_limits(
    defaults: Defaults, overrides: Sequence[Override], principal: str
) -> list[BudgetLimit]:
    """The limits that apply to `principal`, tenant before person.

    For each (scope, window) an override replaces the default: the tenant's limit by the row for
    `subject=''`; a person's by their own row, else by the tenant's `*` row. A row of None is an
    explicit "no cap" (the limit is omitted); any number, including 0, is the cap. With no
    override the default applies when above 0, except tenant/day which is always enforced."""
    by_key = {(o.subject, o.period): o for o in overrides}
    limits: list[BudgetLimit] = []
    for scope, window, subjects, default, always in (
        ("tenant", "day", (TENANT_SUBJECT,), defaults.tenant_day, True),
        ("tenant", "month", (TENANT_SUBJECT,), defaults.tenant_month, False),
        ("principal", "day", (principal, ALL_PRINCIPALS), defaults.principal_day, False),
        ("principal", "month", (principal, ALL_PRINCIPALS), defaults.principal_month, False),
    ):
        value: float | None = default if (always or default > 0) else None
        for subject in subjects:
            row = by_key.get((subject, window))
            if row is not None:
                value = row.limit_usd
                break
        if value is not None:
            limits.append(BudgetLimit(scope, window, value))  # type: ignore[arg-type]  # scope/window are the Literal values above
    return limits


def configured_limits(
    *,
    tenant_day: float,
    tenant_month: float = 0.0,
    principal_day: float = 0.0,
    principal_month: float = 0.0,
) -> list[BudgetLimit]:
    """The limits to enforce from the Settings defaults alone (no overrides), tenant before
    person. The tenant daily limit is always present (a value of 0 there refuses everything, as
    it always has); the others are present only when above 0."""
    return resolve_limits(Defaults(tenant_day, tenant_month, principal_day, principal_month), [], "")


async def _spend(limit: BudgetLimit, ctx: SecurityCtx, now: datetime) -> tuple[float, float]:
    """(spend in the limit's window, in-flight holds) for this limit's scope. The spend is the usage events'
    (`spend.usage_summary`); the holds are `budget_holds`' (concurrency, not history)."""
    principal = ctx["principal"] if limit.scope == "principal" else None
    summary = await spend.usage_summary(
        ctx["tenant"], principal=principal, since=window_start(limit.window, now)
    )
    reserved = await budget_holds.in_flight_reservation(ctx["tenant"]) if limit.scope == "tenant" else 0.0
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


async def _credit_check(ctx: SecurityCtx) -> tuple[Allowance | None, bool]:
    """(a refusal, or None if the gate has no objection; whether the wallet could not be read and the turn was served anyway). Called only
    for a valid ctx. The decision is the billing package's; this maps it (module docstring, "The credit gate")."""
    tenant = ctx["tenant"]
    admission = await billing_port.admit(tenant, reserved_usd=lambda: budget_holds.in_flight_reservation(tenant))
    if admission.status == "exhausted":
        logger.warning("turn_refused_no_credits", extra={"tenant": tenant, "principal": ctx["principal"]})
        return Allowance("insufficient_credits", available_credits=admission.available, reserved_credits=admission.reserved), False
    if admission.status == "unavailable":
        return Allowance("unavailable"), False
    return None, admission.status == "degraded"


async def check_allowance(
    ctx: SecurityCtx | None,
    *,
    limits: Sequence[BudgetLimit],
    fail_policy: str,
    now: datetime | None = None,
) -> Allowance:
    """Spend over each limit's window (the usage events, `spend.usage_summary`) plus, for tenant limits, in-flight holds,
    against that limit.

    An invalid ctx is unattributable, so there is nothing to meter: "ok", without a spend read.
    The first limit at or past its cap refuses the turn ("exceeded"): counted under its scope and
    window, and logged with the tenant and principal, which the counter deliberately has no label
    for. Limits that still allow the turn but have crossed a warning threshold are counted and
    logged as an early signal. Reads are sequential and only made for limits that are enabled, so
    a deployment with just the daily tenant limit pays for exactly one spend read, as before.

    A turn the limits above would serve is then put to the credit gate ("The credit gate" in the module
    docstring), which reads nothing unless the deployment enforces credits.
    """
    if not valid_ctx(ctx):
        return Allowance("ok")
    now = now or datetime.now(UTC)

    spends: list[tuple[float, float]] = []
    try:
        for limit in limits:
            spends.append(await _spend(limit, ctx, now))
    except Exception as exc:  # noqa: BLE001 - a spend read failing must not by itself take every turn down; BUDGET_CHECK_FAILURE_POLICY decides, and it is counted and alerted either way
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

    credit_refusal, credit_degraded = await _credit_check(ctx)
    if credit_refusal is not None:
        return credit_refusal

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
    allowance = closest or Allowance("ok")
    return replace(allowance, degraded=True) if credit_degraded else allowance


async def check(
    ctx: SecurityCtx | None,
    *,
    defaults: Defaults,
    fail_policy: str,
    now: datetime | None = None,
) -> Allowance:
    """`check_allowance` for `ctx` with the operator's overrides applied: reads the tenant's and
    the person's override rows, resolves the limits that apply, and checks them.

    A failed override read is one more way the check cannot answer, and takes the same policy as
    a failed spend read: "closed" refuses the turn as unavailable; "open" serves it against the
    Settings defaults alone — so an override set to SUSPEND someone is not enforced while the read
    is failing — counted (`path="policy_read"`, alert `TenantAllowanceUnenforced`) and marked
    `degraded`. (A missing table is not a failure; see `budget_policies.overrides_for`.)"""
    if not valid_ctx(ctx):
        return Allowance("ok")
    degraded = False
    try:
        overrides = await budget_policies.overrides_for(ctx["tenant"], ctx["principal"])
    except Exception as exc:  # noqa: BLE001 - an override read failing must not by itself take every turn down; BUDGET_CHECK_FAILURE_POLICY decides, and it is counted and alerted either way
        metrics.agent_cost_governance_degraded_total.labels(path="policy_read").inc()
        logger.warning(
            "budget_policy_read_failed",
            extra={"error_class": type(exc).__name__, "fail_policy": fail_policy},
        )
        if fail_policy == "closed":
            return Allowance("unavailable")
        overrides, degraded = [], True
    allowance = await check_allowance(
        ctx,
        limits=resolve_limits(defaults, overrides, ctx["principal"]),
        fail_policy=fail_policy,
        now=now,
    )
    return replace(allowance, degraded=True) if degraded and not allowance.refused else allowance


@dataclass(frozen=True)
class LimitStatus:
    """One enforced limit as `GET /usage` shows it to the caller."""

    scope: Scope
    window: Window
    limit_usd: float
    spent_usd: float
    reserved_usd: float
    resets_at: datetime | None

    @property
    def remaining_usd(self) -> float:
        return max(self.limit_usd - self.spent_usd - self.reserved_usd, 0.0)


async def usage_status(
    ctx: SecurityCtx, *, defaults: Defaults, now: datetime | None = None
) -> list[LimitStatus]:
    """Every limit that applies to `ctx` (overrides included) with its spend, so a caller can see
    how close they are before being refused. Spend includes in-flight holds for tenant limits,
    exactly as the check counts it. Unlike the check this does not fail open: a status endpoint
    that cannot read the spend should say so, not report a calm zero."""
    now = now or datetime.now(UTC)
    overrides = await budget_policies.overrides_for(ctx["tenant"], ctx["principal"])
    statuses = []
    for limit in resolve_limits(defaults, overrides, ctx["principal"]):
        spent, reserved = await _spend(limit, ctx, now)
        statuses.append(
            LimitStatus(limit.scope, limit.window, limit.limit_usd, spent, reserved, window_resets_at(limit.window, now))
        )
    return statuses


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
    if allowance.status == "insufficient_credits":
        # No figure in the message or details: the balance is `GET /usage`'s to report, and an error
        # that echoed it would leak it into every log that keeps error text.
        return ErrorEnvelope(
            code=ErrorCode.INSUFFICIENT_CREDITS,
            message="This organisation has no credits left, so this request was not started. Credits need to be added before it can continue.",
            details={"scope": "tenant"},
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
