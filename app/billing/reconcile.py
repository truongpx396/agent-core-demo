"""Do the numbers agree? Usage events against the ledger, the gateway's spend log and the wallet (specs/010 US6, T026).

Three independent records of the same spending exist, and each can drift from the others for a reason nobody sees
until a customer does:

  * `usage_events`: one row per model call, the billing meter (postgres-init/19);
  * `usage_ledger`: one row per turn, the figure the dollar caps read (postgres-init/03). Both are written from the
    same `PricedCall`, so a difference is a lost write, not a different opinion;
  * the gateway's own spend log (LiteLLM, `end_user` = `gateway.end_user_id(tenant)`): what the provider was actually
    asked to spend. It is written by another process from the request itself, which is what makes it an independent
    second meter (research G5) and the only detector for a call whose event was never written (spec 010 plan: "a model
    call that returns just before a crash is not recorded; the reconciliation is the detector, not a prevention").

and, for a tenant with a wallet, a fourth question: was every event that was worth credits actually debited? The charge
runs in a savepoint so that a wallet fault never loses the meter (D11); the price of that is that an event can exist
uncharged, and "nothing repairs an uncharged event yet" was the gap D11 promised this module would at least NAME.

## What it reports

For each tenant and UTC day, the figures and their difference, in USD, when the difference is above tolerance (a fixed
USD amount or a percentage, whichever is larger: `Tolerance`). The report names the tenant, the day and the amount, so
an operator can go straight to "acme, 2026-10-06, $0.76 of spend the gateway saw and the meter did not".

## What is deliberately not a finding

  * **The newest minutes** (`CREDIT_RECONCILE_SETTLE_SECONDS`): a running turn has events and no ledger row yet, and the
    gateway writes its spend log in batches.
  * **Embeddings.** They are not metered and carry no identity (research G2), so the gateway attributes them to nobody.
    That spend lands in `unattributed_usd`, which is reported but is not drift: it is a known, separate gap.
  * **Another application's end users** on a shared gateway (an `end_user` that is not `tenant_<hash>`): same bucket.

## Known noise (disclosed, not hidden)

A call that STARTS before midnight UTC and is recorded after it lands on different days in the gateway (its start time) and
in the event (the time it was inserted). A busy tenant's tolerance absorbs a call; a tenant with almost no other spend that
day and an expensive call across midnight shows a pair of opposite drifts on adjacent days. That signature is the
straddle, not a loss. The ledger compares the same way (a turn is recorded when it ENDS).

## The gateway read

`GET /spend/logs/v2` with `start_date`/`end_date`, paginated (verified against LiteLLM 1.104's source: a UTC window on
`startTime`, both ends inclusive; `page_size` at most 1000; `total` is capped at 10 000, so the number of pages is never read
from it). The sort has no tie-breaker, so a row could in principle move between pages; rows are therefore de-duplicated by
`request_id`, and when the response's own `total` is below the cap it must equal the rows collected or the read is declared
INCOMPLETE. An incomplete read is never compared: a truncated sum would show every tenant as under-metered and page someone
for nothing. The ceiling is `CREDIT_RECONCILE_GATEWAY_MAX_PAGES`.

## What a pass costs (disclosed)

The events and the ledger are read by TIME, and both tables' indexes lead with the tenant (`usage_events (tenant, occurred_at)`,
`usage_ledger (tenant, recorded_at)`), so a pass scans each table (checked with EXPLAIN: a sequential scan). A time-only index would
make it cheap and would tax the insert of every model call for the benefit of a job that runs a few times a day, so it was not added.
Acceptable for a few passes a day at moderate volume; if a pass becomes slow, that index (or a partition by day) is the fix, and
`CREDIT_RECONCILE_LOOKBACK_DAYS` is the lever until then.

Needs the gateway's admin key (`LITELLM_MASTER_KEY`, read from the environment by the caller, never stored here): spend logs are
an admin view. A narrower key is a follow-up.
"""
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from psycopg import errors as pg_errors

from app.agent import gateway
from app.agent.sql_store import get_connection
from app.billing.display import printable
from app.core import metrics
from app.core.config import (
    CREDIT_RECONCILE_GATEWAY_MAX_PAGES,
    CREDIT_RECONCILE_LOOKBACK_DAYS,
    CREDIT_RECONCILE_SETTLE_SECONDS,
    CREDIT_RECONCILE_TOLERANCE_PCT,
    CREDIT_RECONCILE_TOLERANCE_USD,
)

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
_MILLIONTH = Decimal("0.000001")
GATEWAY_PAGE_SIZE = 1000  # the most `/spend/logs/v2` accepts (`le=1000`)
GATEWAY_PATH = "/spend/logs/v2"
_GATEWAY_TOTAL_CAP = 10_000  # `SPEND_LOGS_PAGINATION_COUNT_CAP` in LiteLLM: a `total` at or above it is a floor, not a count

Key = tuple[str, date]


@dataclass(frozen=True)
class Tolerance:
    """A difference is drift only above `max(usd, pct% of the larger figure)`. Both, because each alone is wrong: a fixed
    amount flags a busy tenant's rounding (the ledger keeps six decimal places a turn and the event twelve a call), a
    percentage flags a cent of noise on a tenant that spent three."""

    usd: Decimal = CREDIT_RECONCILE_TOLERANCE_USD
    pct: Decimal = CREDIT_RECONCILE_TOLERANCE_PCT

    def allows(self, a: Decimal, b: Decimal) -> bool:
        return abs(a - b) <= max(self.usd, max(abs(a), abs(b)) * self.pct / 100)


@dataclass(frozen=True)
class Window:
    """`[start, end]` in UTC, both inclusive (the gateway's own convention, so both sides read the same instants)."""

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
    """Whole UTC days back to midnight, up to `settle_seconds` ago, at whole seconds (the gateway's date format has no
    fraction, so an end with one would be read differently by the two sides)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = (now - timedelta(days=lookback_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now - timedelta(seconds=settle_seconds)).replace(microsecond=0)
    return Window(start, end)


@dataclass(frozen=True)
class Finding:
    """One tenant-day where two records disagree. `expected` is the meter's figure (the events), `actual` the other record's;
    for `uncharged` the expected figure is nothing debited and the actual one is what those events cost."""

    kind: str  # gateway | ledger | uncharged
    tenant: str
    day: date
    expected: Decimal
    actual: Decimal
    note: str = ""

    @property
    def drift(self) -> Decimal:
        return self.actual - self.expected

    @property
    def meaning(self) -> str:
        above = self.drift > 0
        if self.kind == "gateway":
            return (
                "the gateway spent more than the events record: a call whose event was never written, or spend the app does not meter"
                if above
                else "the events record more than the gateway spent: events with no call behind them, or spend the gateway lost"
            )
        if self.kind == "ledger":
            return (
                "the ledger is above the events: a ledger row with no events behind it"
                if above
                else "the events are above the ledger: a turn's ledger write failed (counted as ledger_write), so the dollar caps under-count"
            )
        return "events worth credits that no debit was ever booked for (the wallet failed after the meter kept the event: CreditDebitFailing)"


@dataclass(frozen=True)
class GatewaySpend:
    by_end_user_day: Mapping[tuple[str, date], Decimal]  # an empty end_user is the key "" (no identity was sent)
    rows: int
    complete: bool
    reason: str = ""


@dataclass
class Report:
    window: Window
    tolerance: Tolerance
    findings: list[Finding] = field(default_factory=list)
    tenants: int = 0
    events: int = 0
    gateway_rows: int | None = None
    unattributed_usd: Decimal = ZERO
    skipped: list[str] = field(default_factory=list)
    incomplete: bool = False  # the gateway read could not be trusted to be whole, so that comparison did not run
    outstanding_live: Decimal | None = None
    outstanding_debt: Decimal | None = None

    @property
    def max_drift_usd(self) -> Decimal:
        return max((abs(f.drift) for f in self.findings), default=ZERO)

    @property
    def outcome(self) -> str:
        if self.findings:
            return "drift"
        return "incomplete" if self.incomplete else "ok"

    def to_dict(self) -> dict:
        return {
            "window": {"start": self.window.start.isoformat(), "end": self.window.end.isoformat()},
            "tolerance": {"usd": str(self.tolerance.usd), "pct": str(self.tolerance.pct)},
            "outcome": self.outcome,
            "max_drift_usd": str(self.max_drift_usd),
            "tenants": self.tenants,
            "events": self.events,
            "gateway_rows": self.gateway_rows,
            "unattributed_usd": str(self.unattributed_usd),
            "skipped": self.skipped,
            "findings": [
                {
                    "kind": f.kind, "tenant": f.tenant, "day": f.day.isoformat(), "expected": str(f.expected),
                    "actual": str(f.actual), "drift": str(f.drift), "note": f.note, "meaning": f.meaning,
                }
                for f in self.findings
            ],
        }


def compare(
    kind: str,
    events: Mapping[Key, Decimal],
    other: Mapping[Key, Decimal],
    tolerance: Tolerance,
    notes: Mapping[Key, str] | None = None,
) -> list[Finding]:
    """The tenant-days where `other` differs from `events` by more than the tolerance, largest first. A day present on one
    side only is a difference from zero: that is exactly what a deleted event looks like."""
    findings = [
        Finding(kind, tenant, day, events.get((tenant, day), ZERO), other.get((tenant, day), ZERO), (notes or {}).get((tenant, day), ""))
        for tenant, day in events.keys() | other.keys()
        if not tolerance.allows(events.get((tenant, day), ZERO), other.get((tenant, day), ZERO))
    ]
    return sorted(findings, key=lambda f: (-abs(f.drift), f.tenant, f.day))


def attribute_gateway(
    raw: Mapping[tuple[str, date], Decimal], tenants: Iterable[str]
) -> tuple[dict[Key, Decimal], Decimal]:
    """Gateway spend keyed by tenant, plus the spend that belongs to no tenant of this app.

    The gateway holds a ONE-WAY id (`tenant_<hash>`), so tenants are found by hashing the ones this database knows.
    An id in the app's own format that matches none of them is still a finding, named by that id: it is spend for a
    tenant that has no events and no ledger rows at all, which is the worst shape a lost meter can take. Anything else
    (no identity, or another application's end users) is `unattributed`: reported, never drift."""
    names = {gateway.end_user_id(tenant): tenant for tenant in tenants}
    attributed: dict[Key, Decimal] = {}
    unattributed = ZERO
    for (end_user, day), amount in raw.items():
        if end_user in names:
            tenant = names[end_user]
        elif end_user.startswith("tenant_"):
            tenant = f"{end_user} (no tenant in this database hashes to it)"
        else:
            unattributed += amount
            continue
        attributed[(tenant, day)] = attributed.get((tenant, day), ZERO) + amount
    return attributed, unattributed


def _row_day_and_spend(row: Mapping) -> tuple[str, date, Decimal, str]:
    try:
        start = datetime.fromisoformat(str(row["startTime"]).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return str(row.get("end_user") or ""), start.astimezone(UTC).date(), Decimal(str(row.get("spend") or 0)), str(row["request_id"])
    except (KeyError, ValueError, ArithmeticError, AttributeError, TypeError) as exc:
        raise ValueError(f"a spend-log row the reconciliation cannot read ({type(exc).__name__}): the gateway's format may have changed") from None


def usd(amount: Decimal) -> str:
    """Money for a person: six places, unless the amount is real and smaller than that (a call can cost a fraction of a
    millionth of a dollar), in which case every place it has, so a finding never reads as $0.000000."""
    rounded = amount.quantize(_MILLIONTH)
    return f"{rounded:f}" if rounded != 0 or amount == 0 else f"{amount:f}"


def _gateway_date(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def fetch_gateway_spend(client: httpx.AsyncClient, window: Window, *, max_pages: int = CREDIT_RECONCILE_GATEWAY_MAX_PAGES) -> GatewaySpend:
    """Every spend-log row in the window, summed by end user and UTC day (see the module doc for the endpoint's verified
    behaviour). Raises on an HTTP error or a response that is not the documented shape; returns `complete=False`, with
    the reason, when the read cannot be trusted to be whole."""
    sums: dict[tuple[str, date], Decimal] = {}
    seen: set[str] = set()
    total: int | None = None
    for page in range(1, max_pages + 1):
        response = await client.get(
            GATEWAY_PATH,
            params={
                "start_date": _gateway_date(window.start), "end_date": _gateway_date(window.end), "page": page,
                "page_size": GATEWAY_PAGE_SIZE, "sort_by": "startTime", "sort_order": "asc",
            },
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ValueError(f"the gateway's {GATEWAY_PATH} answered with an unexpected shape: its API may have changed")
        if total is None:
            total = body.get("total") if isinstance(body.get("total"), int) else None
        for row in data:
            end_user, day, amount, request_id = _row_day_and_spend(row)
            if request_id in seen:
                continue
            seen.add(request_id)
            sums[(end_user, day)] = sums.get((end_user, day), ZERO) + amount
        if len(data) < GATEWAY_PAGE_SIZE:
            if total is not None and total < _GATEWAY_TOTAL_CAP and total != len(seen):
                return GatewaySpend(sums, len(seen), False, f"it reported {total} rows and {len(seen)} were read: the log changed while it was being read")
            return GatewaySpend(sums, len(seen), True)
    return GatewaySpend(sums, len(seen), False, f"more than {max_pages * GATEWAY_PAGE_SIZE} rows (CREDIT_RECONCILE_GATEWAY_MAX_PAGES)")


async def events_by_tenant_day(window: Window) -> tuple[dict[Key, Decimal], dict[Key, int], int]:
    """(USD per tenant-day, unpriced events per tenant-day, events in all). An unpriced event adds nothing to the USD
    figure (its cost is unknown, never zero), so it is counted: when the gateway is above the events, an unpriced call is
    the first thing to rule out."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT tenant, (occurred_at AT TIME ZONE 'UTC')::date, COALESCE(SUM(cost_usd), 0), COUNT(*), COUNT(*) FILTER (WHERE cost_usd IS NULL) "
            "FROM usage_events WHERE occurred_at >= %s AND occurred_at <= %s GROUP BY 1, 2",
            (window.start, window.end),
        )
        rows = await cur.fetchall()
    return (
        {(t, d): Decimal(usd) for t, d, usd, _, _ in rows},
        {(t, d): unpriced for t, d, _, _, unpriced in rows if unpriced},
        sum(count for _, _, _, count, _ in rows),
    )


async def ledger_by_tenant_day(window: Window) -> dict[Key, Decimal]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT tenant, (recorded_at AT TIME ZONE 'UTC')::date, COALESCE(SUM(cost_usd), 0) "
            "FROM usage_ledger WHERE recorded_at >= %s AND recorded_at <= %s GROUP BY 1, 2",
            (window.start, window.end),
        )
        return {(t, d): Decimal(usd) for t, d, usd in await cur.fetchall()}


async def wallet_tenants() -> set[str]:
    """Every tenant with a wallet. With the tenants that have events or ledger rows in the window it is the set of names the
    gateway's one-way ids are mapped back to; a small table, so it costs nothing next to the window scans. Empty when the
    wallet tables do not exist (postgres-init/20): credit billing is not in use."""
    async with get_connection() as conn:
        try:
            async with conn.transaction():
                cur = await conn.execute("SELECT tenant FROM credit_accounts")
                return {row[0] for row in await cur.fetchall()}
        except pg_errors.UndefinedTable:
            return set()


async def uncharged_by_tenant_day(window: Window) -> tuple[dict[Key, Decimal], dict[Key, str]]:
    """Events worth credits, for a tenant that had a wallet when they were recorded, with no debit booked. The debit's
    idempotency key IS the event id (`usage_events._charge`), so absence is a lookup on the unique `(tenant, idempotency_key)`
    index. An event recorded before the account existed was rightly never charged, hence `recorded_at >= created_at`."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT e.tenant, (e.occurred_at AT TIME ZONE 'UTC')::date, COALESCE(SUM(e.cost_usd), 0), COUNT(*), SUM(e.credits) "
            "FROM usage_events e JOIN credit_accounts a ON a.tenant = e.tenant "
            "WHERE e.occurred_at >= %s AND e.occurred_at <= %s AND e.credits > 0 AND e.recorded_at >= a.created_at "
            "AND NOT EXISTS (SELECT 1 FROM credit_transactions t WHERE t.tenant = e.tenant AND t.idempotency_key = e.event_id) "
            "GROUP BY 1, 2",
            (window.start, window.end),
        )
        rows = await cur.fetchall()
    return (
        {(t, d): Decimal(usd) for t, d, usd, _, _ in rows},
        {(t, d): f"{count} event(s) worth {credits} credits were never debited" for t, d, _, count, credits in rows},
    )


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


async def reconcile(
    window: Window | None = None,
    *,
    gateway_client: httpx.AsyncClient | None = None,
    tolerance: Tolerance | None = None,
    max_pages: int = CREDIT_RECONCILE_GATEWAY_MAX_PAGES,
) -> Report:
    """One pass. Raises when the events or the ledger cannot be read or the gateway answers wrongly: a pass that did not
    run is a failure to report, never an all-clear. A comparison that cannot run for a stated reason (no wallet tables, an
    incomplete gateway read) is listed in `skipped` instead, and the others still run."""
    window = window or default_window()
    report = Report(window, tolerance or Tolerance())
    if window.empty:
        report.skipped.append("window: it is empty (the settle period reaches back past the start of the day)")
        return report
    events, unpriced, report.events = await events_by_tenant_day(window)
    ledger = await ledger_by_tenant_day(window)
    tenants = {tenant for tenant, _ in events} | {tenant for tenant, _ in ledger} | await wallet_tenants()
    report.tenants = len(tenants)
    report.findings += compare("ledger", events, ledger, report.tolerance)

    if gateway_client is None:
        report.skipped.append("gateway: not compared (not requested)")
    else:
        spend = await fetch_gateway_spend(gateway_client, window, max_pages=max_pages)
        report.gateway_rows = spend.rows
        if not spend.complete:
            report.incomplete = True
            report.skipped.append(f"gateway: not compared, the read is incomplete ({spend.reason})")
        else:
            attributed, report.unattributed_usd = attribute_gateway(spend.by_end_user_day, tenants)
            notes = {key: f"{count} unpriced event(s) in the meter's figure" for key, count in unpriced.items()}
            report.findings += compare("gateway", events, attributed, report.tolerance, notes)

    try:
        uncharged, uncharged_notes = await uncharged_by_tenant_day(window)
        # Compared as "nothing debited" (expected 0) against what those events cost, with no tolerance: a debit
        # is booked or it is not, and a rounding allowance would hide a whole tenant-day of cheap calls.
        report.findings += compare("uncharged", {}, uncharged, Tolerance(ZERO, ZERO), uncharged_notes)
        report.outstanding_live, report.outstanding_debt = await wallet_totals()
    except (pg_errors.UndefinedTable, pg_errors.UndefinedColumn):
        report.skipped.append("wallet: not checked, its tables or the usage_events.credits column are missing (postgres-init/20 and 21)")
    report.findings.sort(key=lambda f: (-abs(f.drift), f.kind, f.tenant, f.day))
    return report


def publish_gauges(report: Report) -> None:
    """The report's point-in-time figures as gauges. Separate from `record_outcome` because the worker calls it again every
    minute: a synchronous gauge is exported once per set and the collector forgets a series five minutes after its last
    update, so a pass every few hours would otherwise be visible to Prometheus (and the alert) for five minutes of each."""
    metrics.agent_credit_reconcile_max_drift_usd.set(float(report.max_drift_usd))
    if report.outstanding_live is not None and report.outstanding_debt is not None:
        metrics.agent_credit_outstanding.labels(state="available").set(float(report.outstanding_live))
        metrics.agent_credit_outstanding.labels(state="debt").set(float(report.outstanding_debt))


OUTCOMES = ("ok", "drift", "incomplete", "failed")


def record_outcome(outcome: str) -> None:
    metrics.agent_credit_reconcile_total.labels(outcome=outcome).inc()


def prime_outcomes() -> None:
    """Creates every outcome's series at zero when the worker starts. Prometheus' `increase()` does not count a series' first
    sample, so without this the FIRST failed pass of a freshly started worker (the likeliest: a wrong key, an unreachable
    gateway) would appear as a series born at 1 and CreditReconcileNotCompleting would never see it."""
    for outcome in OUTCOMES:
        metrics.agent_credit_reconcile_total.labels(outcome=outcome).inc(0)


def render(report: Report) -> str:
    """The report an operator reads. Every finding names the tenant, the day and the amounts."""
    w = report.window
    lines = [
        f"Reconciliation {w.start:%Y-%m-%d %H:%M} to {w.end:%Y-%m-%d %H:%M} UTC; "
        f"tolerance: the larger of ${report.tolerance.usd} and {report.tolerance.pct}%",
        f"  {report.tenants} tenant(s), {report.events} usage event(s), "
        + ("gateway not read" if report.gateway_rows is None else f"{report.gateway_rows} gateway row(s)"),
    ]
    if report.outstanding_live is not None:
        lines.append(f"  credits outstanding: {report.outstanding_live} usable, {report.outstanding_debt} owed")
    if report.unattributed_usd:
        lines.append(f"  gateway spend with no tenant of this app: ${usd(report.unattributed_usd)} (embeddings and other callers; not drift)")
    lines += [f"  skipped: {reason}" for reason in report.skipped]
    if not report.findings:
        lines.append("OK: the records agree within tolerance." if report.outcome == "ok" else "No drift found in what could be compared.")
        return "\n".join(lines)
    lines.append(f"DRIFT in {len(report.findings)} tenant-day(s); largest ${usd(report.max_drift_usd)}:")
    for f in report.findings:
        lines.append(
            f"  [{f.kind}] {printable(f.tenant)}  {f.day}  expected ${usd(f.expected)}  actual ${usd(f.actual)}  drift {'+' if f.drift > 0 else '-'}${usd(abs(f.drift))}"
            + (f"  ({printable(f.note)})" if f.note else "")
        )
        lines.append(f"      {f.meaning}")
    return "\n".join(lines)
