"""The usage-export worker: drains `usage_export_outbox` to the providers that bill on usage
(app/billing/export.py, postgres-init/23-usage-export-outbox.sql; `make billing-export-worker`).

A long-lived loop, like the queue workers: one pass every `BILLING_EXPORT_INTERVAL_SECONDS`, each pass bounded (a batch, a
call deadline, an attempt budget, an age limit; see app/billing/export.py). Several replicas are safe: each pass claims its
batch with `FOR UPDATE SKIP LOCKED`, and the provider is given the usage event's id as its own idempotency key.

It serves only the providers that are enabled (`BILLING_PROVIDERS`) AND declare the USAGE_EXPORT capability. Set
`BILLING_PROVIDERS` the same in the API, the agent workers and this worker: an event is queued only by a process that has the
provider enabled. With nothing to serve it exits at once, saying so, rather than idling and looking healthy.

SIGTERM or SIGINT finishes the pass in flight (its rows are claimed in one transaction, so stopping mid-call would only leave
them pending) and then exits. `--once` runs a single pass and exits, for a cron job or a manual drain.
"""
import argparse
import asyncio
import logging
import signal

from app.agent import sql_store
from app.billing import export, providers
from app.core.config import (
    BILLING_EXPORT_INTERVAL_SECONDS,
    BILLING_PROVIDERS,
    BILLING_WEBHOOK_SECRETS,
)
from app.core.logging_config import configure_logging
from app.core.telemetry import configure_telemetry, shutdown_telemetry

logger = logging.getLogger(__name__)


def serving() -> dict:
    """The adapters this worker drains: enabled, with a secret, and able to take usage. Raises if an enabled provider has no
    adapter or no secret, so a misconfigured deployment stops instead of exporting nothing."""
    names = providers.usage_export_providers(BILLING_PROVIDERS)
    return providers.build_configured(names, BILLING_WEBHOOK_SECRETS)


async def run(*, once: bool) -> int:
    adapters = serving()
    if not adapters:
        print("No enabled billing provider bills on usage (BILLING_PROVIDERS): nothing to export.")
        return 0
    stop = asyncio.Event()
    if not once:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    try:
        while True:
            totals = await export.run_once(adapters)
            logger.info("usage_export_pass", extra={"providers": sorted(adapters), **totals})
            if once:
                print(describe(totals))
                return 0
            try:
                await asyncio.wait_for(stop.wait(), timeout=BILLING_EXPORT_INTERVAL_SECONDS)
                return 0  # stop was set during the wait: shutting down
            except TimeoutError:
                continue
    finally:
        await sql_store.close_pool()


def describe(totals: dict[str, int]) -> str:
    if not any(totals.values()):
        return "Nothing to export."
    return "Export pass: " + ", ".join(f"{count} {outcome}" for outcome, count in sorted(totals.items()) if count) + "."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args()
    configure_logging()
    configure_telemetry("agent-core-billing-export")
    try:
        raise SystemExit(asyncio.run(run(once=args.once)))
    finally:
        shutdown_telemetry()


if __name__ == "__main__":
    main()
