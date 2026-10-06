"""Operator CLI for per-tenant and per-person spend-limit overrides
(app/agent/budget_policies.py, postgres-init/18-budget-policies.sql).

The four spend limits are Settings defaults that apply to everyone. This changes one
tenant's, or one person's, without a redeploy:

    # show a tenant's overrides, and the limits that currently apply to one of its people
    python -m scripts.budget_policy show --tenant acme --principal alice

    # give tenant acme a $50/day tenant limit (its plan tier), audited to you
    python -m scripts.budget_policy set --tenant acme --period day --limit 50 --by you@example.com

    # every person in acme gets a $10/day personal limit; alice gets $25
    python -m scripts.budget_policy set --tenant acme --all-principals --period day --limit 10 --by you
    python -m scripts.budget_policy set --tenant acme --principal alice --period day --limit 25 --by you

    # SUSPEND someone (a limit of 0 refuses every turn), then lift it
    python -m scripts.budget_policy set --tenant acme --principal mallory --period day --limit 0 --by you
    python -m scripts.budget_policy clear --tenant acme --principal mallory --period day

    # explicitly NO cap for a tenant, overriding a Settings default
    python -m scripts.budget_policy set --tenant internal --period month --limit none --by you

`--limit` is a dollar amount, `0` (suspend) or `none` (no cap). A running worker picks a change
up within BUDGET_POLICY_REFRESH_SECONDS (30 by default), so a suspend is bounded, not instant.
This is the one place that writes the table, and it spans tenants by design: it is an operator
tool, never reachable from a request.
"""
import argparse
import asyncio
import sys

from app.agent import budget_policies, budgets
from app.agent.sql_store import close_pool
from app.core.config import (
    MAX_COST_USD_PER_PRINCIPAL_PER_DAY,
    MAX_COST_USD_PER_PRINCIPAL_PER_MONTH,
    MAX_COST_USD_PER_TENANT_PER_DAY,
    MAX_COST_USD_PER_TENANT_PER_MONTH,
)


def _parse_limit(text: str) -> float | None:
    if text.strip().lower() == "none":
        return None
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--limit must be a dollar amount, 0 or 'none', not {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("--limit must not be negative (use 0 to suspend, 'none' for no cap)")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="budget_policy", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def subject_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--tenant", required=True)
        who = p.add_mutually_exclusive_group()
        who.add_argument("--principal", help="one person's limit (default: the tenant's own)")
        who.add_argument("--all-principals", action="store_true", help="the personal limit of every person in the tenant")

    show = sub.add_parser("show", help="list a tenant's overrides")
    show.add_argument("--tenant", required=True)
    show.add_argument("--principal", help="also print the limits that apply to this person")

    set_ = sub.add_parser("set", help="set or replace one override")
    subject_args(set_)
    set_.add_argument("--period", required=True, choices=budget_policies.PERIODS)
    set_.add_argument("--limit", required=True, type=_parse_limit, help="dollars, 0 to suspend, or 'none' for no cap")
    set_.add_argument("--by", required=True, help="who is making the change (recorded)")

    clear = sub.add_parser("clear", help="remove one override, restoring the Settings default")
    subject_args(clear)
    clear.add_argument("--period", required=True, choices=budget_policies.PERIODS)
    return parser


def _subject(args: argparse.Namespace) -> str:
    if args.all_principals:
        return budget_policies.ALL_PRINCIPALS
    if args.principal is None:
        return budget_policies.TENANT_SUBJECT
    if args.principal in budget_policies.RESERVED_SUBJECTS:
        raise SystemExit(f"error: {args.principal!r} is reserved and cannot be a principal id")
    return args.principal


def _describe(limit: float | None) -> str:
    return "no cap" if limit is None else ("0 (suspended: every turn is refused)" if limit == 0 else f"${limit:g}")


def _label(subject: str) -> str:
    return {"": "tenant", "*": "every person"}.get(subject, f"person {subject}")


async def _show(args: argparse.Namespace) -> int:
    rows = await budget_policies.list_overrides(args.tenant)
    print(f"Overrides for tenant {args.tenant!r}:" if rows else f"No overrides for tenant {args.tenant!r}.")
    for row in rows:
        print(f"  {_label(row.subject):<22} {row.period:<6} {_describe(row.limit_usd)}")
    if args.principal:
        budget_policies.reset_cache()
        overrides = await budget_policies.overrides_for(args.tenant, args.principal)
        defaults = budgets.Defaults(
            MAX_COST_USD_PER_TENANT_PER_DAY,
            MAX_COST_USD_PER_TENANT_PER_MONTH,
            MAX_COST_USD_PER_PRINCIPAL_PER_DAY,
            MAX_COST_USD_PER_PRINCIPAL_PER_MONTH,
        )
        print(f"Limits that apply to {args.principal!r} in {args.tenant!r}:")
        for limit in budgets.resolve_limits(defaults, overrides, args.principal) or []:
            print(f"  {limit.scope:<10} {limit.window:<6} ${limit.limit_usd:g}")
    return 0


async def _set(args: argparse.Namespace) -> int:
    subject = _subject(args)
    await budget_policies.set_override(args.tenant, subject, args.period, args.limit, args.by)
    print(f"Set {args.period} limit for {_label(subject)} in tenant {args.tenant!r}: {_describe(args.limit)}.")
    print("A running worker applies it within BUDGET_POLICY_REFRESH_SECONDS (30 by default).")
    return 0


async def _clear(args: argparse.Namespace) -> int:
    subject = _subject(args)
    removed = await budget_policies.clear_override(args.tenant, subject, args.period)
    print(
        f"Cleared the {args.period} override for {_label(subject)} in tenant {args.tenant!r}; the Settings default applies again."
        if removed
        else f"No {args.period} override for {_label(subject)} in tenant {args.tenant!r}; nothing to clear."
    )
    return 0


async def run(argv: list[str] | None = None) -> int:
    """Runs one command and returns the process exit code. The connection pool is closed
    whatever happens, so the process exits promptly."""
    args = _build_parser().parse_args(argv)
    try:
        return await {"show": _show, "set": _set, "clear": _clear}[args.command](args)
    finally:
        await close_pool()


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
