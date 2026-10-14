"""Operator CLI for the credit wallet: grant, adjust, show (credit_billing/credits.py; specs/010 T027; `make credits`, or `credit-billing` once the package is installed).

    # give a tenant credits (opens its wallet if it has none); a promo or subscription grant MUST expire
    make credits ARGS="grant --tenant acme --amount 500 --by alice --reason 'pilot top-up, ticket 4412'"
    make credits ARGS="grant --tenant acme --amount 100 --source promo --expires-in-days 30 --by alice --reason 'launch promo'"

    # a signed correction: positive adds credits, negative takes them back (it can leave the wallet in debt)
    make credits ARGS="adjust --tenant acme --amount -12.5 --by alice --reason 'call billed twice, event 3f9c...'"

    # read-only: the balance, every lot, and the newest ledger entries
    make credits ARGS="show --tenant acme --entries 40"

Every change names WHO did it (`--by`) and WHY (`--reason`), both required and both stored on the transaction and printed by
`show`: the ledger is a financial record and a hand-made entry nobody can explain is the one an audit cannot accept. `show`
changes nothing, so it asks for neither.

**A retry is safe only with the same `--key`.** Each change carries an idempotency key; without `--key` one is generated and
PRINTED. Run the same command again with `--key <that key>` and a change that already landed is reported as a duplicate and
changes nothing; run it again WITHOUT the key and it is, deliberately, a second change.

**A key stands for one request.** Run it again with the same `--key` but something different (another amount, source, reason, expiry)
and it is refused, naming each field that differs, with exit status 2 and nothing changed: answering "already applied" would hide that
what you just typed was never recorded. `--expires-in-days` is recomputed on every run, so a retry a minute later differs by a minute;
give the first run's exact instant as `--expires-at 2026-11-13T07:20:11+00:00` (printed in the conflict message) to retry exactly.
`--expires-at` and `--expires-in-days` cannot be combined.

Credits are exact decimals with at most six places. An amount is never read as a float.

Exit status: 0 done (or a duplicate of a change that already landed), 1 nothing changed (no such wallet), 2 invalid input (including a
reused key with a different request), 3 the
database could not be reached (within a few seconds, with a one-line `error:` naming host, port and database, never the password).
"""
import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from credit_billing import credits, db
from credit_billing.cli._runtime import (
    EXIT_DATABASE_UNAVAILABLE,
    DatabaseUnavailable,
    scheduled_job,
    wired_pool,
)
from credit_billing.display import printable

logger = logging.getLogger(__name__)

# 'adjustment' is reserved for `adjust`, which is the only command that records it as one.
GRANT_CHOICES = sorted(credits.GRANT_SOURCES - {"adjustment"})
MAX_ACTOR_LENGTH = 80
MAX_ENTRIES = 200


def _amount(text: str) -> Decimal:
    try:
        return credits.credits_amount(Decimal(text))
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite number") from None


def _n(value: Decimal) -> str:
    """Credits as a person writes them: 500, not 500.000000 (the column keeps six places; JSON output keeps them all)."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _by(text: str) -> str:
    name = text.strip()
    if not name or len(name) > MAX_ACTOR_LENGTH or not name.isprintable():
        raise argparse.ArgumentTypeError(f"--by must be a short printable name (1 to {MAX_ACTOR_LENGTH} characters)")
    return name


def _reason(text: str) -> str:
    reason = text.strip()
    if not reason or not reason.isprintable():
        raise argparse.ArgumentTypeError("--reason must say why, in printable text")
    return reason


def _instant(text: str) -> datetime:
    """`--expires-at`: an ISO 8601 instant that says which zone it is in. A time without an offset is refused rather than read as local or
    UTC, because a guess here is a lot that expires hours early or late."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an ISO 8601 time, for example 2026-11-13T07:20:11+00:00") from None
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{text!r} has no UTC offset: write it as 2026-11-13T07:20:11+00:00 (or with a trailing Z)")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="credits", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def audited(command: argparse.ArgumentParser) -> None:
        command.add_argument("--tenant", required=True)
        command.add_argument("--by", required=True, type=_by, help="who is making this change (stored as operator:<name>)")
        command.add_argument("--reason", required=True, type=_reason, help="why: stored on the transaction and shown by `show`")
        command.add_argument("--key", help="idempotency key; generated and printed when omitted (pass it again to retry safely)")

    grant = sub.add_parser("grant", help="add credits to a tenant (opens its wallet if it has none)")
    audited(grant)
    grant.add_argument("--amount", required=True, type=_amount, help="credits to add, above zero, up to 6 decimal places")
    grant.add_argument("--source", choices=GRANT_CHOICES, default="manual", help="what the credits are (default manual); promo and subscription must expire")
    when = grant.add_mutually_exclusive_group()
    when.add_argument("--expires-in-days", type=int, help="the lot expires this many days from now (recomputed on every run: use --expires-at to retry exactly)")
    when.add_argument("--expires-at", type=_instant, help="the lot expires at this instant, ISO 8601 with a UTC offset, e.g. 2026-11-13T07:20:11+00:00")

    adjust = sub.add_parser("adjust", help="a signed correction: positive adds credits, negative takes them back")
    audited(adjust)
    adjust.add_argument("--amount", required=True, type=_amount, help="credits, signed and not zero (write a negative one as --amount=-5)")
    adjust_when = adjust.add_mutually_exclusive_group()
    adjust_when.add_argument("--expires-in-days", type=int, help="positive adjustments only: the credits added expire this many days from now")
    adjust_when.add_argument("--expires-at", type=_instant, help="positive adjustments only: the credits added expire at this instant (ISO 8601 with a UTC offset)")

    show = sub.add_parser("show", help="a tenant's balance, lots and newest ledger entries (read-only)")
    show.add_argument("--tenant", required=True)
    show.add_argument("--entries", type=int, default=20, help=f"how many of the newest ledger entries to list (default 20, at most {MAX_ENTRIES})")
    show.add_argument("--json", action="store_true", dest="as_json", help="print as JSON")
    return parser


def _expiry(args: argparse.Namespace) -> datetime | None:
    if args.expires_at is not None:
        return args.expires_at
    days = args.expires_in_days
    if days is None:
        return None
    if days < 1:
        raise ValueError("--expires-in-days must be at least 1")
    return datetime.now(UTC) + timedelta(days=days)


def _outcome(verb: str, applied: credits.Applied, args: argparse.Namespace, key: str) -> tuple[int, str]:
    """What the operator reads, and the exit code. `no_account` is the one result that is not a success: a negative
    adjustment has nothing to take from, and the tool will not open a wallet to hold a debt."""
    if applied.status == "no_account":
        return 1, f"Nothing changed: {args.tenant!r} has no credit wallet, so there is nothing to take credits from."
    if applied.status == "duplicate":
        return 0, f"Already applied under key {key} (transaction {applied.transaction_id}); nothing changed."
    note = f" ({_n(applied.shortfall)} of it is now debt, repaid first by the next grant)" if applied.shortfall else ""
    logger.info(
        "credit_operator_action",
        extra={"action": verb, "tenant": args.tenant, "actor": f"operator:{args.by}", "amount": str(applied.amount), "key": key},
    )
    return 0, f"{verb.capitalize()} applied: {_n(applied.amount)} credits, tenant {args.tenant!r}, transaction {applied.transaction_id}{note}.\nIdempotency key: {key}"


def _conflict_message(conflict: credits.IdempotencyConflict) -> str:
    """One line per field that differs, then what to do. Values are shown through `printable`: a reason or a reference may have come from
    somewhere other than this terminal, and text is data, never something to interpret."""
    lines = [f"error: idempotency key {printable(conflict.key)!r} was already used for a different request:"]
    lines += [f"  {field}: {printable(first)!r} -> {printable(now)!r}" for field, (first, now) in conflict.differences.items()]
    lines.append("nothing changed. A retry must send exactly the same request; a different request needs a new --key.")
    first_expiry = conflict.differences.get("expires_at", (None, None))[0]
    if first_expiry:
        lines.append(f"to repeat the first request exactly, run it again with --key {printable(conflict.key)} --expires-at {printable(first_expiry)}")
    return "\n".join(lines)


async def _change(args: argparse.Namespace) -> tuple[int, str]:
    key = args.key or f"cli:{uuid.uuid4()}"
    actor = f"operator:{args.by}"
    try:
        expires_at = _expiry(args)
        if args.command == "grant":
            applied = await credits.grant(
                args.tenant, args.amount, source=args.source, idempotency_key=key, actor=actor, reason=args.reason, expires_at=expires_at
            )
        else:
            applied = await credits.adjust(
                args.tenant, args.amount, idempotency_key=key, actor=actor, reason=args.reason, expires_at=expires_at
            )
    except credits.IdempotencyConflict as exc:
        return 2, _conflict_message(exc)
    except ValueError as exc:
        return 2, f"error: {exc}"
    return _outcome(args.command, applied, args, key)


def entries_limit(requested: int) -> int:
    """How many ledger entries `show` reads: what was asked for, at least one and at most MAX_ENTRIES (the table grows with
    every model call of a tenant, so an unbounded read is a way to take the database down by typing a big number)."""
    return max(1, min(requested, MAX_ENTRIES))


async def _show(args: argparse.Namespace) -> tuple[int, str]:
    entries = entries_limit(args.entries)
    async with db.get_connection() as conn:
        balance = await credits.account_balance_in(conn, args.tenant)
        if balance is None:
            return 1, f"{args.tenant!r} has no credit wallet (it is not on credit billing)."
        problems = await credits.verify_in(conn, args.tenant)
        lots = await (
            await conn.execute(
                "SELECT id, source, granted, remaining, expires_at, created_at, created_by, provider, external_ref FROM credit_lots "
                "WHERE tenant = %s ORDER BY created_at DESC, id LIMIT 100", (args.tenant,)
            )
        ).fetchall()
        rows = await (
            await conn.execute(
                "SELECT e.created_at, t.kind, e.lot_id, e.amount, t.actor, t.reason, t.usage_event_id FROM credit_entries e "
                "JOIN credit_transactions t ON t.id = e.transaction_id AND t.tenant = e.tenant WHERE e.tenant = %s "
                "ORDER BY e.id DESC LIMIT %s", (args.tenant, entries)
            )
        ).fetchall()
    if args.as_json:
        return 0, json.dumps(
            {
                "tenant": args.tenant,
                "balance": {"available": str(balance.available), "ledger": str(balance.ledger), "debt": str(balance.debt)},
                "problems": problems,
                "lots": [
                    {"id": str(r[0]), "source": r[1], "granted": str(r[2]), "remaining": str(r[3]), "expires_at": r[4].isoformat() if r[4] else None,
                     "created_at": r[5].isoformat(), "created_by": r[6], "provider": r[7], "external_ref": r[8]}
                    for r in lots
                ],
                "entries": [
                    {"at": r[0].isoformat(), "kind": r[1], "lot_id": str(r[2]), "amount": str(r[3]), "actor": r[4], "reason": r[5], "usage_event_id": r[6]}
                    for r in rows
                ],
            },
            indent=2,
        )
    lines = [
        f"Tenant {args.tenant!r}",
        f"  available {_n(balance.available)}   ledger {_n(balance.ledger)}   debt {_n(balance.debt)}",
        "  consistent: every lot's remaining equals the sum of its entries" if not problems else "  INCONSISTENT:",
        *[f"    {line}" for line in problems],
        f"Lots ({len(lots)}, newest first):",
        *[
            f"  {str(r[0])[:8]}  {r[1]:<10} granted {_n(r[2])}  remaining {_n(r[3])}  "
            + (f"expires {r[4]:%Y-%m-%d %H:%M}Z" if r[4] else "no expiry") + f"  by {printable(r[6])}"
            + (f"  via {printable(r[7])} {printable(r[8])}" if r[7] or r[8] else "")
            for r in lots
        ],
        f"Newest {len(rows)} ledger entries:",
        *[
            f"  {r[0]:%Y-%m-%d %H:%M:%S}Z  {r[1]:<8} {_n(r[3]):>14}  lot {str(r[2])[:8]}  {printable(r[4])}"
            + (f"  {printable(r[5])}" if r[5] else "") + (f"  event {r[6][:8]}" if r[6] else "")
            for r in rows
        ],
    ]
    return 0, "\n".join(lines)


async def execute(args: argparse.Namespace) -> tuple[int, str]:
    return await (_show(args) if args.command == "show" else _change(args))


async def _run(args: argparse.Namespace) -> tuple[int, str]:
    try:
        async with wired_pool():
            return await execute(args)
    except DatabaseUnavailable as exc:
        return EXIT_DATABASE_UNAVAILABLE, f"error: {exc}"


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    with scheduled_job("agent-core-credits-cli"):
        code, text = asyncio.run(_run(args))
    print(text, file=sys.stderr if code else sys.stdout)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
