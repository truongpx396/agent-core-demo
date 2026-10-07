"""Reconcile usage events against the ledger, the gateway's spend log and the wallets (app/billing/reconcile.py;
specs/010 T026; `make credit-reconcile`, `make credit-reconcile-worker`).

    # one pass over today and yesterday (UTC), printed; exit 0 = records agree, 1 = drift, 2 = could not finish
    make credit-reconcile
    make credit-reconcile ARGS="--days 7 --json"
    make credit-reconcile ARGS="--no-gateway"     # only the app's own records; no LITELLM_MASTER_KEY needed

    # a worker: a pass every CREDIT_RECONCILE_INTERVAL_SECONDS, publishing the figures the dashboard and the alerts read
    make credit-reconcile-worker

The gateway comparison reads LiteLLM's spend log with the gateway's admin key, taken from `LITELLM_MASTER_KEY` in the
environment (never an argument) and the gateway's address from `LITELLM_URL`, the same two things `scripts/litellm_key.py`
reads. The worker REFUSES to start without them unless `--no-gateway` is given: a worker that quietly skipped the
independent meter would report "ok" while proving less than its name says.

Why a worker and not just cron: the figures reach Prometheus through an OpenTelemetry collector whose Prometheus exporter
forgets a series five minutes after its last update, and a synchronous gauge is exported once per set. A pass every few hours
would show the drift gauge for five minutes in each of them, so an alert on it could never stay firing. The worker re-sets the
gauges from the last pass every minute. Cron with `make credit-reconcile` still works for the printed report and the exit
code, but it gives the alert nothing durable to hold on to (disclosed in infra/README.md).

SIGTERM or SIGINT stops the worker after the pass in flight.
"""
import argparse
import asyncio
import json
import logging
import os
import signal

import httpx

from app.agent import sql_store
from app.agent.model_resolver import admin_base_url
from app.billing import reconcile
from app.core.config import (
    CREDIT_RECONCILE_INTERVAL_SECONDS,
    CREDIT_RECONCILE_LOOKBACK_DAYS,
)
from app.core.logging_config import configure_logging
from app.core.telemetry import configure_telemetry, shutdown_telemetry

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60  # well inside the collector's five-minute series expiry


def gateway_client(*, no_gateway: bool) -> httpx.AsyncClient | None:
    if no_gateway:
        return None
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not key:
        raise SystemExit(
            "error: LITELLM_MASTER_KEY must be set in the environment to read the gateway's spend log "
            "(it is deliberately not an argument); pass --no-gateway to compare only the app's own records"
        )
    return httpx.AsyncClient(
        base_url=os.environ.get("LITELLM_URL") or admin_base_url(), headers={"Authorization": f"Bearer {key}"}, timeout=30
    )


def exit_code(report: reconcile.Report | None) -> int:
    """0 the records agree, 1 drift, 2 the pass could not finish (it raised, or the gateway read was incomplete)."""
    if report is None or report.outcome == "incomplete":
        return 2
    return 1 if report.outcome == "drift" else 0


async def run_pass(client: httpx.AsyncClient | None, *, days: int) -> reconcile.Report | None:
    """One pass, never raising: a failure is counted (`failed`, alert CreditReconcileNotCompleting) and logged by CLASS
    only, since an HTTP or database error's text can carry an address or a statement."""
    try:
        report = await reconcile.reconcile(reconcile.default_window(lookback_days=days), gateway_client=client)
    except Exception as exc:  # noqa: BLE001 - a pass that cannot run is the failure to report, not a reason to stop the worker
        reconcile.record_outcome("failed")
        logger.warning("credit_reconcile_failed", extra={"error_class": type(exc).__name__})
        return None
    reconcile.publish_gauges(report)
    reconcile.record_outcome(report.outcome)
    return report


async def run(*, loop: bool, days: int, as_json: bool, no_gateway: bool) -> int:
    client = gateway_client(no_gateway=no_gateway)
    reconcile.prime_outcomes()
    stop = asyncio.Event()
    if loop:
        running = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            running.add_signal_handler(sig, stop.set)
    try:
        while True:
            report = await run_pass(client, days=days)
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
    finally:
        if client is not None:
            await client.aclose()
        await sql_store.close_pool()


def _describe(report: reconcile.Report | None, *, as_json: bool) -> str:
    if report is None:
        return "Reconciliation failed: see the log (credit_reconcile_failed). Nothing was proven either way."
    if as_json:
        return json.dumps(report.to_dict(), indent=2)
    return reconcile.render(report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--loop", action="store_true", help="run as a worker: a pass every CREDIT_RECONCILE_INTERVAL_SECONDS")
    parser.add_argument("--days", type=int, default=CREDIT_RECONCILE_LOOKBACK_DAYS, help="whole UTC days to look back (default CREDIT_RECONCILE_LOOKBACK_DAYS)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="print the report as JSON")
    parser.add_argument("--no-gateway", action="store_true", help="skip the gateway comparison (compare the app's own records only)")
    args = parser.parse_args()
    if not 1 <= args.days <= 35:
        parser.error("--days must be between 1 and 35")
    configure_logging()
    configure_telemetry("agent-core-credit-reconcile")
    try:
        raise SystemExit(asyncio.run(run(loop=args.loop, days=args.days, as_json=args.as_json, no_gateway=args.no_gateway)))
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
