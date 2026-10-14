"""Operator command that applies the billing package's schema scripts to an existing database (`credit_billing/migrate.py`; spec 012; `make billing-migrate`).

    credit-billing-migrate                          # apply what is pending or changed, in order
    credit-billing-migrate --status                 # what is applied and what is not; writes nothing
    credit-billing-migrate --check                  # the same, and exit 1 if anything is pending or changed: a deploy gate
    credit-billing-migrate --lock-timeout-seconds 30

Safe to run again: a second run applies nothing. Safe to run twice at once: each script is applied once. Safe on a database the agent app
initialised: the scripts are idempotent, so applying one to a database that already has it changes no row, and then it is recorded.

Exit status: 0 done, up to date, or a `--status`; 1 `--check` found a script pending or changed; 2 invalid arguments; 3 the database could not be
reached (within a few seconds, one `error:` line naming host, port and database, never the password); 4 a migration did not complete (a lock
wait timed out or a script failed; nothing from that script was applied or recorded, and running again is safe).

The connection comes from `APPDATA_DATABASE_URL` as in the other operator commands. Nothing runs this by itself: not on import, not on
application start, not in the container image.
"""
import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC

from credit_billing import migrate
from credit_billing.cli._runtime import (
    EXIT_DATABASE_UNAVAILABLE,
    DatabaseUnavailable,
    scheduled_job,
    wired_pool,
)

EXIT_CHECK_FOUND_WORK = 1
EXIT_NOT_COMPLETE = 4

_NO_RECORD = (
    "No migration record in this database yet. Scripts show as pending even where the schema already exists (a database the agent app "
    "initialised has it). Running this command is safe: the scripts are idempotent, and applying them is how an existing database is adopted."
)


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be above zero, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="credit-billing-migrate", description="Apply the billing package's schema scripts to an existing database.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="show what is applied and what is not; write nothing")
    mode.add_argument("--check", action="store_true", help="like --status, and exit 1 if any script is pending or changed")
    parser.add_argument(
        "--lock-timeout-seconds", type=_positive_seconds, default=migrate.DEFAULT_LOCK_TIMEOUT_SECONDS,
        help=f"give up if a lock cannot be had within this many seconds (default {migrate.DEFAULT_LOCK_TIMEOUT_SECONDS:g})",
    )
    return parser


def format_status(status: migrate.Status) -> str:
    lines = []
    for script in status.scripts:
        if script.state == "applied" and script.applied_at is not None:
            lines.append(f"{'applied':<8} {script.name}   {script.applied_at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}")
        elif script.state == "changed":
            lines.append(f"{'changed':<8} {script.name}   recorded {(script.recorded_checksum or '')[:8]}…  shipped {script.checksum[:8]}…")
        else:
            lines.append(f"{script.state:<8} {script.name}")
    lines.extend(f"{'unknown':<8} {name}   recorded by another version; left alone" for name in status.unknown)
    if not status.record_table_exists:
        lines.append(_NO_RECORD)
    return "\n".join(lines)


def format_result(result: migrate.Result) -> str:
    """One line per script in the order they are applied in, then the totals."""
    position = {script.name: index for index, script in enumerate(migrate.shipped())}
    named = [("applied", name) for name in result.applied] + [("current", name) for name in result.skipped]
    lines = [f"{label:<8} {name}" for label, name in sorted(named, key=lambda item: position.get(item[1], len(position)))]
    lines.append(f"{len(result.applied)} applied, {len(result.skipped)} already current.")
    return "\n".join(lines)


async def execute(args: argparse.Namespace) -> tuple[int, str]:
    if args.status or args.check:
        status = await migrate.status()
        return (EXIT_CHECK_FOUND_WORK if args.check and not status.up_to_date else 0), format_status(status)
    return 0, format_result(await migrate.migrate(lock_timeout_seconds=args.lock_timeout_seconds))


async def _run(args: argparse.Namespace) -> tuple[int, str]:
    try:
        async with wired_pool():
            return await execute(args)
    except DatabaseUnavailable as exc:
        return EXIT_DATABASE_UNAVAILABLE, f"error: {exc}"
    except migrate.MigrationError as exc:
        return EXIT_NOT_COMPLETE, f"error: {exc}"


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    with scheduled_job("agent-core-billing-migrate"):
        code, text = asyncio.run(_run(args))
    print(text, file=sys.stderr if code not in (0, EXIT_CHECK_FOUND_WORK) else sys.stdout)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
