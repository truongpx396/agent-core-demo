"""Does the meter agree with the LLM gateway? The agent's usage events against the gateway's spend log (specs/010
US6, T026, T030c; the wallet half moved to the billing package in specs/013).

Two independent records of the same spending, each able to drift unseen: `usage_events` (the meter and the
spend the caps sum), and the gateway's spend log (LiteLLM, `end_user` = `gateway.end_user_id(tenant,
principal)`), written by another process from the request itself. The gateway is the only detector for a call
whose event was never written (the reconciliation detects, it doesn't prevent).

It reports, per tenant and UTC day, the figures and their USD difference above `Tolerance` (a fixed amount or
a percentage, whichever is larger), e.g. "acme, 2026-10-06, $0.76 of spend the gateway saw and the meter did
not".

Deliberately not findings: the newest minutes (`USAGE_RECONCILE_SETTLE_SECONDS`; the gateway logs in batches);
embeddings (unmetered, no identity, so they land in `unattributed_usd`); another application's end users on a
shared gateway (same bucket).

Known noise: a call that STARTS before midnight UTC and is recorded after it lands on different days in the
gateway (start time) and the event (insert time). A busy tenant's tolerance absorbs it; a quiet tenant with an
expensive call shows opposite drifts on adjacent days, the straddle signature, not a loss.

The gateway read is `GET /spend/logs/v2` with `start_date`/`end_date`, paginated (checked against LiteLLM
1.104: UTC window on `startTime`, both ends inclusive, `page_size` at most 1000, `total` capped at 10 000 so
the page count never comes from it). The sort has no tie-breaker, so rows are de-duplicated by `request_id`,
and a `total` below the cap must equal the rows collected or the read is INCOMPLETE. An incomplete read is
never compared, since a truncated sum would page someone for every tenant. Ceiling:
`USAGE_RECONCILE_GATEWAY_MAX_PAGES`.

Cost: events are read by time but the index leads with the tenant, so a pass is a sequential scan. A time-only
index would tax every insert for a job that runs a few times a day; if a pass gets slow, that index (or a
daily partition) is the fix and `USAGE_RECONCILE_LOOKBACK_DAYS` the lever.

Needs the gateway admin key (`LITELLM_MASTER_KEY`, read from the environment by the caller, never stored
here), since spend logs are an admin view.
"""
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from app.core import gateway, metrics
from app.core.config import (
    USAGE_RECONCILE_GATEWAY_MAX_PAGES,
    USAGE_RECONCILE_LOOKBACK_DAYS,
    USAGE_RECONCILE_SETTLE_SECONDS,
    USAGE_RECONCILE_TOLERANCE_PCT,
    USAGE_RECONCILE_TOLERANCE_USD,
)
from app.core.sql_store import get_connection

logger = logging.getLogger(__name__)

ZERO = Decimal(0)
_MILLIONTH = Decimal("0.000001")
GATEWAY_PAGE_SIZE = 1000  # the most `/spend/logs/v2` accepts (`le=1000`)
GATEWAY_PATH = "/spend/logs/v2"
_GATEWAY_TOTAL_CAP = 10_000  # `SPEND_LOGS_PAGINATION_COUNT_CAP` in LiteLLM: a `total` at or above it is a floor, not a count

Key = tuple[str, date]


def printable(value: object) -> str:
    """Text from the database, made safe to print on an operator's terminal: anything not printable is shown as its escape (`\\x1b`), which is
    visible and inert. A tenant name comes from an identity token and an operator's terminal obeys escape sequences."""
    text = "" if value is None else str(value)
    return text if text.isprintable() else text.encode("unicode_escape").decode("ascii")


@dataclass(frozen=True)
class Tolerance:
    """A difference is drift only above `max(usd, pct% of the larger figure)`. Both, because each alone is wrong: a fixed
    amount flags a busy tenant's rounding (the gateway and the events round differently), a
    percentage flags a cent of noise on a tenant that spent three."""

    usd: Decimal = USAGE_RECONCILE_TOLERANCE_USD
    pct: Decimal = USAGE_RECONCILE_TOLERANCE_PCT

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
    lookback_days: int = USAGE_RECONCILE_LOOKBACK_DAYS,
    settle_seconds: int = USAGE_RECONCILE_SETTLE_SECONDS,
) -> Window:
    """Whole UTC days back to midnight, up to `settle_seconds` ago, at whole seconds (the gateway's date format has no
    fraction, so an end with one would be read differently by the two sides)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = (now - timedelta(days=lookback_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now - timedelta(seconds=settle_seconds)).replace(microsecond=0)
    return Window(start, end)


@dataclass(frozen=True)
class Finding:
    """One tenant-day where two records disagree. `expected` is the meter's figure (the events), `actual` the gateway's."""

    kind: str  # gateway
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
        return (
            "the gateway spent more than the events record: a call whose event was never written, or spend the app does not meter"
            if above
            else "the events record more than the gateway spent: events with no call behind them, or spend the gateway lost"
        )


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

    The gateway's end user names the PERSON (`tenant:<tenant>/user:<principal>`), and the reconciliation is per tenant, so
    each person's spend is summed into their tenant's day; the tenant is read off the id, not looked up. A tenant the gateway
    spent for and this database has no events for needs no special case: `compare` already reports a day present on one
    side only as a difference from zero, which is the worst shape a lost meter can take.

    Rows written before the end user named the person carry the older one-way id (`tenant_<hash>`, `gateway.legacy_end_user_id`),
    which is still in the spend logs for as long as the gateway keeps them: those tenants are found by hashing the ones this
    database knows, and one that matches none is still a finding, named by that id. Anything else (no identity, or another
    application's end users) is `unattributed`: reported, never drift."""
    legacy = {gateway.legacy_end_user_id(tenant): tenant for tenant in tenants}
    attributed: dict[Key, Decimal] = {}
    unattributed = ZERO
    for (end_user, day), amount in raw.items():
        person = gateway.parse_end_user_id(end_user)
        if person is not None:
            tenant = person[0]
        elif end_user in legacy:
            tenant = legacy[end_user]
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


async def fetch_gateway_spend(client: httpx.AsyncClient, window: Window, *, max_pages: int = USAGE_RECONCILE_GATEWAY_MAX_PAGES) -> GatewaySpend:
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
    return GatewaySpend(sums, len(seen), False, f"more than {max_pages * GATEWAY_PAGE_SIZE} rows (USAGE_RECONCILE_GATEWAY_MAX_PAGES)")


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


async def known_tenants() -> set[str]:
    """Every tenant that has ever had a usage event. With the tenants that have events in the window it is the set of names the gateway's
    one-way ids are mapped back to: a tenant with no event in the window but gateway spend in it is the shape a lost meter takes. The
    table's index leads with the tenant, so this is an index-only scan; a few passes a day cost nothing next to the window scan."""
    async with get_connection() as conn:
        cur = await conn.execute("SELECT DISTINCT tenant FROM usage_events")
        return {row[0] for row in await cur.fetchall()}


async def reconcile(
    window: Window | None = None,
    *,
    gateway_client: httpx.AsyncClient | None = None,
    tolerance: Tolerance | None = None,
    max_pages: int = USAGE_RECONCILE_GATEWAY_MAX_PAGES,
) -> Report:
    """One pass. Raises when the events cannot be read or the gateway answers wrongly: a pass that did not
    run is a failure to report, never an all-clear. A comparison that cannot run for a stated reason (an incomplete gateway read, or none
    requested) is listed in `skipped` instead."""
    window = window or default_window()
    report = Report(window, tolerance or Tolerance())
    if window.empty:
        report.skipped.append("window: it is empty (the settle period reaches back past the start of the day)")
        return report
    events, unpriced, report.events = await events_by_tenant_day(window)
    tenants = {tenant for tenant, _ in events} | await known_tenants()
    report.tenants = len(tenants)

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

    report.findings.sort(key=lambda f: (-abs(f.drift), f.kind, f.tenant, f.day))
    return report


def publish_gauges(report: Report) -> None:
    """The report's point-in-time figure as a gauge. Separate from `record_outcome` because the worker calls it again every
    minute: a synchronous gauge is exported once per set and the collector forgets a series five minutes after its last
    update, so a pass every few hours would otherwise be visible to Prometheus (and the alert) for five minutes of each."""
    metrics.agent_usage_reconcile_max_drift_usd.set(float(report.max_drift_usd))


OUTCOMES = ("ok", "drift", "incomplete", "failed")


def record_outcome(outcome: str) -> None:
    metrics.agent_usage_reconcile_total.labels(outcome=outcome).inc()


def prime_outcomes() -> None:
    """Creates every outcome's series at zero when the worker starts. Prometheus' `increase()` does not count a series' first
    sample, so without this the FIRST failed pass of a freshly started worker (the likeliest: a wrong key, an unreachable
    gateway) would appear as a series born at 1 and UsageReconcileNotCompleting would never see it."""
    for outcome in OUTCOMES:
        metrics.agent_usage_reconcile_total.labels(outcome=outcome).inc(0)


def render(report: Report) -> str:
    """The report an operator reads. Every finding names the tenant, the day and the amounts."""
    w = report.window
    lines = [
        f"Reconciliation {w.start:%Y-%m-%d %H:%M} to {w.end:%Y-%m-%d %H:%M} UTC; "
        f"tolerance: the larger of ${report.tolerance.usd} and {report.tolerance.pct}%",
        f"  {report.tenants} tenant(s), {report.events} usage event(s), "
        + ("gateway not read" if report.gateway_rows is None else f"{report.gateway_rows} gateway row(s)"),
    ]
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
