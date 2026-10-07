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

## Overrides

The four limits are Settings defaults that apply to everyone. `budget_policies` (a table an
operator edits) overrides them per tenant, per person, or for every person in one tenant:
`resolve_limits` applies a person's row, else the tenant's `*` row, else the default. An
override of NULL means "no cap" and 0 means "refuse everything" (the suspend switch); see
postgres-init/18-budget-policies.sql. The override read is one more read the check can fail,
and it follows the same failure policy as the ledger read.

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

## The credit gate (specs/010 T016)

A deployment that sells credits adds one more kind of limit, and it is a different KIND: not a ceiling
that resets, but a prepaid balance that only a grant refills. When `CREDITS_ENFORCEMENT` is on, a tenant
that HAS a wallet (a `credit_accounts` row) is refused, before any model work, with
`ErrorCode.INSUFFICIENT_CREDITS` once its available credits minus its in-flight holds are not positive.
A tenant with no wallet is never gated (spec D8): shipping this changes nothing for anyone until an
operator, or a verified purchase, opens one.

  * **Holds are expressed in credits.** Each running turn holds `MAX_COST_USD_PER_TURN` dollars
    (`usage_ledger.reserve_budget`); the gate converts that to credits at the configured rate, so a
    tenant with 100 credits cannot start a second concurrent turn that may spend 500. The first turn of
    a tenant is checked against the balance alone, so it can overdraw by at most one turn's worth, which
    the wallet books as debt instead of refusing (`credits.debit_in`) and which the next grant repays.
  * **It runs after the dollar limits**, so an operator's spend cap is what a caller is told about when
    both apply: buying credits would not help a tenant the cap is stopping. Only a turn the dollar limits
    would serve reads the wallet, so a refused turn pays for no extra read.
  * **Off means untouched.** `credit_gate=None` (enforcement off) reads nothing: the wallet is not
    queried, so the turn path costs exactly what it did before (SC-005).
  * **A wallet that cannot be read** is governed by `CREDIT_CHECK_FAILURE_POLICY`, separately from the
    ledger's because the two can fail separately. "open" serves the turn and counts it
    (`agent_cost_governance_degraded_total{path="credit_read"}`, alert `CreditGateUnenforced`); "closed"
    refuses it as `budget_check_unavailable`, which blames no one's balance. The in-flight hold read
    stays open to 0.0 either way, as above.

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from app.agent import budget_policies, usage_ledger
from app.billing import credits
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


@dataclass(frozen=True)
class CreditGate:
    """What the credit gate needs, bound at call time like `Defaults` (runtime.py). Passing one means
    enforcement is on: `credits_per_usd` and `markup` turn the dollar holds into credits, and
    `fail_policy` ("open" | "closed") says what to do when the wallet cannot be read."""

    credits_per_usd: Decimal
    markup: Decimal
    fail_policy: str


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
    defaults: Defaults, overrides: Sequence[budget_policies.Override], principal: str
) -> list[BudgetLimit]:
    """The limits that apply to `principal`, tenant before person.

    For each (scope, window) an override replaces the default: the tenant's limit by the row for
    `subject=''`; a person's by their own row, else by the tenant's `*` row. A row of None is an
    explicit "no cap" (the limit is omitted); any number, including 0, is the cap. With no
    override the default applies when above 0, except tenant/day which is always enforced."""
    by_key = {(o.subject, o.period): o for o in overrides}
    limits: list[BudgetLimit] = []
    for scope, window, subjects, default, always in (
        ("tenant", "day", (budget_policies.TENANT_SUBJECT,), defaults.tenant_day, True),
        ("tenant", "month", (budget_policies.TENANT_SUBJECT,), defaults.tenant_month, False),
        ("principal", "day", (principal, budget_policies.ALL_PRINCIPALS), defaults.principal_day, False),
        ("principal", "month", (principal, budget_policies.ALL_PRINCIPALS), defaults.principal_month, False),
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


async def _credit_check(ctx: SecurityCtx, gate: CreditGate) -> tuple[Allowance | None, bool]:
    """(a refusal, or None if the gate has no objection; whether the wallet could not be read and the
    turn was served anyway). Called only for a valid ctx."""
    tenant = ctx["tenant"]
    try:
        wallet = await credits.account_balance(tenant)
    except Exception as exc:  # noqa: BLE001 - a wallet read failing must not by itself take every turn down; CREDIT_CHECK_FAILURE_POLICY decides, and it is counted and alerted either way
        metrics.agent_cost_governance_degraded_total.labels(path="credit_read").inc()
        logger.warning(
            "credit_check_failed", extra={"error_class": type(exc).__name__, "fail_policy": gate.fail_policy}
        )
        return (Allowance("unavailable"), False) if gate.fail_policy == "closed" else (None, True)
    if wallet is None:
        return None, False  # no wallet: not on credit billing, never gated (spec D8)
    reserved = credits.credits_for_cost(await usage_ledger.in_flight_reservation(tenant), gate.credits_per_usd, gate.markup)
    if wallet.available - reserved > 0:
        return None, False
    metrics.agent_credit_enforcement_refused_total.inc()
    logger.warning(
        "credits_exhausted",
        extra={
            "tenant": tenant,
            "principal": ctx["principal"],
            "available_credits": str(wallet.available),
            "reserved_credits": str(reserved),
        },
    )
    return Allowance("insufficient_credits", available_credits=wallet.available, reserved_credits=reserved), False


async def check_allowance(
    ctx: SecurityCtx | None,
    *,
    limits: Sequence[BudgetLimit],
    fail_policy: str,
    now: datetime | None = None,
    credit_gate: CreditGate | None = None,
) -> Allowance:
    """Spend over each limit's window (`usage_ledger`) plus, for tenant limits, in-flight holds,
    against that limit.

    An invalid ctx is unattributable, so there is nothing to meter: "ok", without a ledger read.
    The first limit at or past its cap refuses the turn ("exceeded"): counted under its scope and
    window, and logged with the tenant and principal, which the counter deliberately has no label
    for. Limits that still allow the turn but have crossed a warning threshold are counted and
    logged as an early signal. Reads are sequential and only made for limits that are enabled, so
    a deployment with just the daily tenant limit pays for exactly one ledger read, as before.

    With a `credit_gate`, a turn the limits above would serve is then checked against the tenant's
    wallet ("The credit gate" in the module docstring); without one the wallet is never read.
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

    credit_refusal, credit_degraded = await _credit_check(ctx, credit_gate) if credit_gate else (None, False)
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
    credit_gate: CreditGate | None = None,
) -> Allowance:
    """`check_allowance` for `ctx` with the operator's overrides applied: reads the tenant's and
    the person's override rows, resolves the limits that apply, and checks them.

    A failed override read is one more way the check cannot answer, and takes the same policy as
    a failed ledger read: "closed" refuses the turn as unavailable; "open" serves it against the
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
        credit_gate=credit_gate,
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
    that cannot read the ledger should say so, not report a calm zero."""
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
