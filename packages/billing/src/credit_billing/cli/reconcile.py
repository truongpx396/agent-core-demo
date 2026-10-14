"""Reconcile the wallet: was every usage worth credits actually charged? (credit_billing/reconcile.py; `credit-billing-reconcile`,
`make credit-reconcile`.)

    # one pass over the last CREDIT_RECONCILE_LOOKBACK_DAYS (UTC), printed; exit 0 = agree, 1 = drift, 2 = could not finish, 3 = database unreachable
    credit-billing-reconcile
    credit-billing-reconcile --days 7 --json

    # a worker: a pass every CREDIT_RECONCILE_INTERVAL_SECONDS, publishing the figures the dashboard and the alerts read
    credit-billing-reconcile --loop

Why a worker and not just cron: the figures reach Prometheus through an OpenTelemetry collector whose Prometheus exporter forgets a series
five minutes after its last update, and a synchronous gauge is exported once per set. A pass every few hours would show the drift gauge for
five minutes in each of them, so an alert on it could never stay firing. The worker re-sets the gauges from the last pass every minute. Cron
still works for the printed report and the exit code, but it gives the alert nothing durable to hold on to.

SIGTERM or SIGINT stops the worker after the pass in flight.
"""
import argparse
import asyncio
import json
import logging
import signal
import sys

from credit_billing import reconcile
from credit_billing.cli._runtime import (
    EXIT_DATABASE_UNAVAILABLE,
    DatabaseUnavailable,
    scheduled_job,
    wired_pool,
)
from credit_billing.settings import (
    CREDIT_RECONCILE_INTERVAL_SECONDS,
    CREDIT_RECONCILE_LOOKBACK_DAYS,
)

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60  # well inside the collector's five-minute series expiry
EXIT_DRIFT = 1
EXIT_NOT_COMPLETE = 2


def exit_code(report: reconcile.Report | None) -> int:
    """0 every usage worth credits was charged, 1 some was not, 2 the pass could not finish (it raised)."""
    if report is None:
        return EXIT_NOT_COMPLETE
    return EXIT_DRIFT if report.outcome == "drift" else 0


async def run_pass(*, days: int) -> reconcile.Report | None:
    """One pass, never raising: a failure is counted (`failed`, alert CreditReconcileNotCompleting) and logged by CLASS only, since a
    database error's text can carry an address or a statement."""
    try:
        report = await reconcile.reconcile(reconcile.default_window(lookback_days=days))
    except Exception as exc:  # noqa: BLE001 - a pass that cannot run is the failure to report, not a reason to stop the worker
        reconcile.record_outcome("failed")
        logger.warning("credit_reconcile_failed", extra={"error_class": type(exc).__name__})
        return None
    reconcile.publish_gauges(report)
    reconcile.record_outcome(report.outcome)
    return report


def _describe(report: reconcile.Report | None, *, as_json: bool) -> str:
    if report is None:
        return "Reconciliation failed: see the log (credit_reconcile_failed). Nothing was proven either way."
    if as_json:
        return json.dumps(report.to_dict(), indent=2)
    return reconcile.render(report)


async def run(*, loop: bool, days: int, as_json: bool) -> int:
    reconcile.prime_outcomes()
    stop = asyncio.Event()
    if loop:
        running = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            running.add_signal_handler(sig, stop.set)
    async with wired_pool():
        while True:
            report = await run_pass(days=days)
            print(_describe(report, as_json=as_json), flush=True)
            if not loop:
                return exit_code(report)
            waited = 0
            while waited < CREDIT_RECONCILE_INTERVAL_SECONDS:
                step = min(HEARTBEAT_SECONDS, CREDIT_RECONCILE_INTERVAL_SECONDS - waited)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=step)
                    return 0  # stop was set during the wait: shutting down
                except TimeoutError:
                    waited += step
                if report is not None:
                    reconcile.publish_gauges(report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--loop", action="store_true", help="run as a worker: a pass every CREDIT_RECONCILE_INTERVAL_SECONDS")
    parser.add_argument("--days", type=int, default=CREDIT_RECONCILE_LOOKBACK_DAYS, help="whole UTC days to look back (default CREDIT_RECONCILE_LOOKBACK_DAYS)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print the report as JSON")
    args = parser.parse_args()
    if not 1 <= args.days <= 35:
        parser.error("--days must be between 1 and 35")
    with scheduled_job("credit-billing-reconcile"):
        try:
            raise SystemExit(asyncio.run(run(loop=args.loop, days=args.days, as_json=args.as_json)))
        except DatabaseUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_DATABASE_UNAVAILABLE) from exc


if __name__ == "__main__":
    main()
