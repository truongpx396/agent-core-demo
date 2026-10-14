"""The usage-export worker: drains `billing_export_outbox` to the providers that bill on usage
(credit_billing/export.py, sql/25-billing-export-outbox.sql; `credit-billing-export-worker`, `make billing-export-worker`).

A long-lived loop, like a queue worker: one pass every `BILLING_EXPORT_INTERVAL_SECONDS`, each pass bounded (a batch, a call deadline, an
attempt budget, an age limit; see credit_billing/export.py). Several replicas are safe: each pass claims its batch with
`FOR UPDATE SKIP LOCKED`, and the provider is given the usage event's id as its own idempotency key.

It serves only the providers that are enabled (`BILLING_PROVIDERS`) AND declare the USAGE_EXPORT capability. Set `BILLING_PROVIDERS` the
same in every process that records usage and in this worker: an event is queued only by a process that has the provider enabled. With
nothing to serve it exits at once, saying so, rather than idling and looking healthy.

SIGTERM or SIGINT finishes the pass in flight (its rows are claimed in one transaction, so stopping mid-call would only leave them
pending) and then exits. `--once` runs a single pass and exits, for a cron job or a manual drain. Exits 3 with a one-line `error:` when the
database cannot be reached.
"""
import argparse
import asyncio
import logging
import signal
import sys

from credit_billing import export, providers
from credit_billing.cli._runtime import (
    EXIT_DATABASE_UNAVAILABLE,
    DatabaseUnavailable,
    scheduled_job,
    wired_pool,
)
from credit_billing.providers.base import BillingProvider
from credit_billing.settings import (
    BILLING_EXPORT_INTERVAL_SECONDS,
    BILLING_PROVIDERS,
    BILLING_WEBHOOK_SECRETS,
)

logger = logging.getLogger(__name__)


def serving() -> dict[str, BillingProvider]:
    """The adapters this worker drains: enabled, with a secret, and able to take usage. Raises if an enabled provider has no adapter or no
    secret, so a misconfigured deployment stops instead of exporting nothing."""
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
    async with wired_pool():
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


def describe(totals: dict[str, int]) -> str:
    if not any(totals.values()):
        return "Nothing to export."
    return "Export pass: " + ", ".join(f"{count} {outcome}" for outcome, count in sorted(totals.items()) if count) + "."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args()
    with scheduled_job("credit-billing-export-worker"):
        try:
            raise SystemExit(asyncio.run(run(once=args.once)))
        except DatabaseUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_DATABASE_UNAVAILABLE) from exc


if __name__ == "__main__":
    main()
