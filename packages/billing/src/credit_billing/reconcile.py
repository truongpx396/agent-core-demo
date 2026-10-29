"""Was every usage that was worth credits actually charged? The wallet reconciliation (specs/010 US6 and T026;
moved here from the app's reconciliation by specs/013).

`usage.record_usage_in` keeps a usage event even when the debit after it fails, on purpose: a lost record
can't be repaired and an uncharged one can. The price is that an event can exist uncharged with nothing in the
request path saying so but a counter. This check NAMES it: for a tenant that had a wallet when the usage was
recorded, every `billing_usage_events` row with `credits > 0` must have a debit in `credit_transactions` whose
idempotency key is the event id.

It reports, per tenant and UTC day, how many such events there are, what they're worth and cost ("acme,
2026-10-06, 12 events worth 340 credits were never debited"), plus the credits outstanding across every wallet
(usable and owed) for the dashboard.

Deliberately not findings:
- **The newest minutes** (`CREDIT_RECONCILE_SETTLE_SECONDS`): a debit shares its event's transaction, but a
  pass reads at its own instant and can't see one in flight.
- **Usage recorded before the tenant had a wallet** (`recorded_at >= created_at`): rightly never charged.
- **An unpriced or free call** (`credits` NULL or 0): nothing was owed.

It does not check that a debit is for the right AMOUNT (the ledger's `verify` checks the lots), nor that every
usage the CALLER metered reached this package: a record never handed over is invisible here. That is what the
caller's own comparison with its upstream is for (the app compares its events with the gateway's spend log)
and what `BillingEventWriteFailing` pages on.

Cost: events are read by TIME (the index is on `occurred_at`), then the wallet's unique `(tenant,
idempotency_key)` index is probed once per event. Fine for a few passes a day;
`CREDIT_RECONCILE_LOOKBACK_DAYS` is the lever.

Imports nothing from `app` (enforced by tests/test_boundary.py).
"""
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from credit_billing import metrics
from credit_billing.db import get_connection
from credit_billing.display import printable
from credit_billing.settings import (
    CREDIT_RECONCILE_LOOKBACK_DAYS,
    CREDIT_RECONCILE_SETTLE_SECONDS,
)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
_MILLIONTH = Decimal("0.000001")

OUTCOMES = metrics.RECONCILE_OUTCOMES


@dataclass(frozen=True)
class Window:
    """`[start, end]` in UTC, both inclusive."""

    start: datetime
    end: datetime

    @property
    def empty(self) -> bool:
        return self.end < self.start


def default_window(
    now: datetime | None = None,
    *,
    lookback_days: int = CREDIT_RECONCILE_LOOKBACK_DAYS,
    settle_seconds: int = CREDIT_RECONCILE_SETTLE_SECONDS,
) -> Window:
    """Whole UTC days back to midnight, up to `settle_seconds` ago, at whole seconds."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = (now - timedelta(days=lookback_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now - timedelta(seconds=settle_seconds)).replace(microsecond=0)
    return Window(start, end)


@dataclass(frozen=True)
class Finding:
    """One tenant-day with usage worth credits that no debit was booked for."""

    tenant: str
    day: date
    events: int
    credits: Decimal
    usd: Decimal

    @property
    def meaning(self) -> str:
        return "usage worth credits that no debit was ever booked for (the wallet failed after the event was kept: CreditDebitFailing)"


@dataclass
class Report:
    window: Window
    findings: list[Finding] = field(default_factory=list)
    tenants: int = 0
    events: int = 0
    skipped: list[str] = field(default_factory=list)
    outstanding_live: Decimal | None = None
    outstanding_debt: Decimal | None = None

    @property
    def max_drift_usd(self) -> Decimal:
        return max((f.usd for f in self.findings), default=ZERO)

    @property
    def outcome(self) -> str:
        return "drift" if self.findings else "ok"

    def to_dict(self) -> dict:
        return {
            "window": {"start": self.window.start.isoformat(), "end": self.window.end.isoformat()},
            "outcome": self.outcome,
            "max_drift_usd": str(self.max_drift_usd),
            "tenants": self.tenants,
            "events": self.events,
            "skipped": self.skipped,
            "outstanding": (
                None if self.outstanding_live is None else {"available": str(self.outstanding_live), "debt": str(self.outstanding_debt)}
            ),
            "findings": [
                {
                    "tenant": f.tenant, "day": f.day.isoformat(), "events": f.events, "credits": str(f.credits), "usd": str(f.usd),
                    "meaning": f.meaning,
                }
                for f in self.findings
            ],
        }


async def uncharged_by_tenant_day(window: Window) -> list[Finding]:
    """Events worth credits, for a tenant that had a wallet when they were recorded, with no debit booked. The debit's idempotency key IS
    the event id (`usage._charge`), so absence is a lookup on the unique `(tenant, idempotency_key)` index. Largest first."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT e.tenant, (e.occurred_at AT TIME ZONE 'UTC')::date, COALESCE(SUM(e.cost_usd), 0), COUNT(*), SUM(e.credits) "
            "FROM billing_usage_events e JOIN credit_accounts a ON a.tenant = e.tenant "
            "WHERE e.occurred_at >= %s AND e.occurred_at <= %s AND e.credits > 0 AND e.recorded_at >= a.created_at "
            "AND NOT EXISTS (SELECT 1 FROM credit_transactions t WHERE t.tenant = e.tenant AND t.idempotency_key = e.event_id) "
            "GROUP BY 1, 2",
            (window.start, window.end),
        )
        rows = await cur.fetchall()
    findings = [Finding(tenant, day, int(count), Decimal(credits), Decimal(usd)) for tenant, day, usd, count, credits in rows]
    return sorted(findings, key=lambda f: (-f.usd, f.tenant, f.day))


async def events_in(window: Window) -> tuple[int, int]:
    """(events, tenants) recorded in the window, for the report's header."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT tenant) FROM billing_usage_events WHERE occurred_at >= %s AND occurred_at <= %s",
            (window.start, window.end),
        )
        row = await cur.fetchone()
    assert row is not None  # an aggregate always returns a row
    return int(row[0]), int(row[1])


async def wallet_totals(tenant: str | None = None) -> tuple[Decimal, Decimal]:
    """(credits still usable, credits owed on overdraft lots), across every wallet, or one tenant's when named."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(SUM(remaining) FILTER (WHERE source <> 'overdraft' AND (expires_at IS NULL OR expires_at > now())), 0), "
            "COALESCE(-SUM(remaining) FILTER (WHERE source = 'overdraft'), 0) FROM credit_lots WHERE (%(tenant)s::text IS NULL OR tenant = %(tenant)s)",
            {"tenant": tenant},
        )
        row = await cur.fetchone()
    assert row is not None  # an aggregate always returns a row
    return Decimal(row[0]), Decimal(row[1])


async def reconcile(window: Window | None = None) -> Report:
    """One pass. Raises when the database cannot be read or the package's tables are missing: a pass that did not run is a failure to
    report, never an all-clear."""
    window = window or default_window()
    report = Report(window)
    if window.empty:
        report.skipped.append("window: it is empty (the settle period reaches back past the start of the day)")
        return report
    report.events, report.tenants = await events_in(window)
    report.findings = await uncharged_by_tenant_day(window)
    report.outstanding_live, report.outstanding_debt = await wallet_totals()
    return report


def publish_gauges(report: Report) -> None:
    """The report's point-in-time figures as gauges. Separate from `record_outcome` because the worker calls it again every minute: a
    synchronous gauge is exported once per set and the collector forgets a series five minutes after its last update, so a pass every few
    hours would otherwise be visible to Prometheus (and the alert) for five minutes of each."""
    metrics.agent_credit_reconcile_max_drift_usd.set(float(report.max_drift_usd))
    if report.outstanding_live is not None and report.outstanding_debt is not None:
        metrics.agent_credit_outstanding.labels(state="available").set(float(report.outstanding_live))
        metrics.agent_credit_outstanding.labels(state="debt").set(float(report.outstanding_debt))


def record_outcome(outcome: str) -> None:
    metrics.agent_credit_reconcile_total.labels(outcome=outcome).inc()


def prime_outcomes() -> None:
    """Creates every outcome's series at zero when the worker starts. Prometheus' `increase()` does not count a series' first sample, so
    without this the FIRST failed pass of a freshly started worker (the likeliest: an unreachable database) would appear as a series born
    at 1 and CreditReconcileNotCompleting would never see it."""
    for outcome in OUTCOMES:
        metrics.agent_credit_reconcile_total.labels(outcome=outcome).inc(0)


def usd(amount: Decimal) -> str:
    """Money for a person: six places, unless the amount is real and smaller than that (a call can cost a fraction of a millionth of a
    dollar), in which case every place it has, so a finding never reads as $0.000000."""
    rounded = amount.quantize(_MILLIONTH)
    return f"{rounded:f}" if rounded != 0 or amount == 0 else f"{amount:f}"


def render(report: Report) -> str:
    """The report an operator reads. Every finding names the tenant, the day and the amounts."""
    w = report.window
    lines = [
        f"Wallet reconciliation {w.start:%Y-%m-%d %H:%M} to {w.end:%Y-%m-%d %H:%M} UTC",
        f"  {report.tenants} tenant(s), {report.events} usage event(s)",
    ]
    if report.outstanding_live is not None:
        lines.append(f"  credits outstanding: {report.outstanding_live} usable, {report.outstanding_debt} owed")
    lines += [f"  skipped: {reason}" for reason in report.skipped]
    if not report.findings:
        lines.append("OK: every usage worth credits was charged.")
        return "\n".join(lines)
    lines.append(f"DRIFT in {len(report.findings)} tenant-day(s); largest ${usd(report.max_drift_usd)}:")
    for f in report.findings:
        lines.append(f"  [uncharged] {printable(f.tenant)}  {f.day}  {f.events} event(s) worth {f.credits} credits (${usd(f.usd)}) were never debited")
        lines.append(f"      {f.meaning}")
    return "\n".join(lines)
