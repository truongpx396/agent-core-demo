"""Operator CLI for the app's scoped LiteLLM key — the gateway-side spend backstop.

By default the app sends LiteLLM's MASTER key. That is gateway admin: it can mint keys, read every
spend log, rewrite models and remove budgets, and it cannot carry a budget of its own — so an app
that sends it has no gateway-side spend cap, and compromising one container hands over the
gateway. This mints a key that can only call the models the app uses, with a budget, a time
window and rate limits, so LiteLLM itself stops runaway spend (answering `budget_exceeded`,
which the app reports as `provider_budget_exceeded` and alerts on) even if every ceiling inside
the app has failed.

    # mint it ONCE (the master key comes from the environment, never from an argument)
    LITELLM_MASTER_KEY=sk-... python -m scripts.litellm_key create \\
        --max-budget 600 --budget-duration 30d --rpm-limit 600 --tpm-limit 400000

    # how much of it is used, and when it resets
    LITELLM_MASTER_KEY=sk-... LITELLM_APP_KEY=sk-... python -m scripts.litellm_key info

    # the end-user id the gateway shows for a person (readable: tenant:<tenant>/user:<principal>); without
    # --principal it prints the older tenant-level id (tenant_<hash>) that spend logs written before the
    # end user named the person still carry
    python -m scripts.litellm_key end-user --tenant acme --principal alice
    python -m scripts.litellm_key end-user --tenant acme

    # the default per-person cap (COST_GOVERNANCE.md pattern 74): create or change it, then read it back
    LITELLM_MASTER_KEY=sk-... python -m scripts.litellm_key person-budget set --max-budget 20 --budget-duration 30d
    LITELLM_MASTER_KEY=sk-... python -m scripts.litellm_key person-budget info

Sizing `--max-budget`: it is the LAST line of defence, so set it ABOVE what the app's own limits can
legitimately add up to (the sum of your tenants' monthly caps), so it fires only when they have
failed. A budget below normal use turns routine traffic into an outage. The per-person budget is
sized the same way against ONE person: above the largest single person's legitimate spend for the
same window (the app's own per-person caps, `MAX_COST_USD_PER_PRINCIPAL_*`, are off by default).

The key is printed once, to stdout, and is never logged; put it in LITELLM_APP_KEY.

The per-person budget is one budget OBJECT in LiteLLM's database that the configs name
(`litellm_settings.max_end_user_budget_id`, the same id as PERSON_BUDGET_ID below) and that LiteLLM
applies to every end user (every person: the app sends `user` = `tenant:<tenant>/user:<principal>`)
with no budget of its own. It caps each PERSON, not a tenant: a tenant with many people can spend
many times it, and the tenant's own limits are the app's (`MAX_COST_USD_PER_TENANT_*`). The config
only names the budget. If it does not exist, LiteLLM finds nothing, logs a warning on each call and
applies NO cap, so `person-budget info` exits 1 when it is missing: run it after a deploy. It is
read through a 60-second cache, so a change takes up to a minute.
"""
import argparse
import os
import sys

import httpx

from app.adapters.litellm.model_catalog import admin_base_url
from app.core.gateway import end_user_id, legacy_end_user_id

# Every alias deploy/litellm/litellm-config.prod.yaml defines (tests/scripts/test_litellm_key.py keeps the two in step). A key lists the
# aliases it may call, and an alias missing here is refused for the app, so the tiers an operator attaches later would fail with
# "key not allowed to access model" until the key was re-minted. Naming one the app does not use yet grants nothing: the budget still applies.
DEFAULT_MODELS = "chat,chat-medium,chat-strong,embed"

# The id of the default per-person budget object. The LiteLLM configs name it in `max_end_user_budget_id`; tests/core/test_litellm_config_prices.py
# checks both do, so the name and the object cannot drift apart.
PERSON_BUDGET_ID = "person-default"


def _master_key() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not key:
        raise SystemExit("error: LITELLM_MASTER_KEY must be set in the environment (it is deliberately not an argument)")
    return key


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=os.environ.get("LITELLM_URL") or admin_base_url(),
        headers={"Authorization": f"Bearer {_master_key()}"},
        timeout=15,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="litellm_key", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="mint the app's scoped, budget-capped key")
    create.add_argument("--max-budget", type=float, required=True, help="USD the key may spend per window (no default: it is a business number)")
    create.add_argument("--budget-duration", default="30d", help="how often the spend resets, e.g. 30d (default), 7d, 1mo")
    create.add_argument("--rpm-limit", type=int, help="requests per minute across the key")
    create.add_argument("--tpm-limit", type=int, help="tokens per minute across the key")
    create.add_argument("--max-parallel", type=int, help="concurrent requests across the key")
    create.add_argument("--models", default=DEFAULT_MODELS, help=f"comma-separated aliases the key may call (default {DEFAULT_MODELS}, the ones deploy/litellm/litellm-config.prod.yaml defines); list every alias the app calls")
    create.add_argument("--alias", default="agent-core-app", help="a name to find the key by in LiteLLM's UI")

    sub.add_parser("info", help="spend, budget and reset time of the key in LITELLM_APP_KEY")

    end_user = sub.add_parser("end-user", help="the end-user id the gateway shows for a person (with --principal) or the older tenant-level one (without)")
    end_user.add_argument("--tenant", required=True)
    end_user.add_argument("--principal", help="the person; omit it to get the tenant_<hash> id that spend logs written before the end user named the person carry")

    person_budget = sub.add_parser("person-budget", help=f"the default per-person spend cap at the gateway (budget {PERSON_BUDGET_ID!r})")
    actions = person_budget.add_subparsers(dest="action", required=True)
    set_budget = actions.add_parser("set", help="create the budget, or change it if it exists")
    set_budget.add_argument("--max-budget", type=float, required=True, help="USD one person may spend per window (no default: it is a business number)")
    set_budget.add_argument("--budget-duration", default="30d", help="how often the spend resets, e.g. 30d (default), 7d, 1mo")
    actions.add_parser("info", help="read it back; exits 1 if it does not exist (then LiteLLM applies no per-person cap)")
    return parser


def _create(args: argparse.Namespace) -> int:
    if args.max_budget <= 0:
        raise SystemExit("error: --max-budget must be above 0 (a key with no budget is exactly what this replaces)")
    payload: dict = {
        "key_alias": args.alias,
        "models": [m.strip() for m in args.models.split(",") if m.strip()],
        "max_budget": args.max_budget,
        "budget_duration": args.budget_duration,
        "metadata": {"purpose": "agent-core-demo app key", "created_by": "scripts/litellm_key.py"},
    }
    for field, value in (("rpm_limit", args.rpm_limit), ("tpm_limit", args.tpm_limit), ("max_parallel_requests", args.max_parallel)):
        if value is not None:
            payload[field] = value
    with _client() as client:
        response = client.post("/key/generate", json=payload)
    if response.status_code != 200:
        print(f"error: LiteLLM refused to mint the key (HTTP {response.status_code}): {response.text[:300]}", file=sys.stderr)
        return 1
    key = response.json().get("key")
    if not key:
        print("error: LiteLLM answered without a key", file=sys.stderr)
        return 1
    print(f"Created key {args.alias!r}: ${args.max_budget:g} per {args.budget_duration}, models {payload['models']}.")
    print("Shown once; put it in LITELLM_APP_KEY (deploy/env/prod.env.example):")
    print(key)
    return 0


def _info(args: argparse.Namespace) -> int:
    app_key = os.environ.get("LITELLM_APP_KEY", "")
    if not app_key:
        raise SystemExit("error: LITELLM_APP_KEY must be set in the environment")
    with _client() as client:
        response = client.get("/key/info", params={"key": app_key})
    if response.status_code != 200:
        print(f"error: LiteLLM refused (HTTP {response.status_code}): {response.text[:300]}", file=sys.stderr)
        return 1
    info = response.json().get("info", {})
    spend, budget = info.get("spend"), info.get("max_budget")
    print(f"Key {info.get('key_alias')!r}")
    print(f"  spent:     ${spend:.4f}" if isinstance(spend, int | float) else "  spent:     ?")
    print(f"  budget:    ${budget:g} per {info.get('budget_duration')}" if budget is not None else "  budget:    NONE (this key is uncapped)")
    print(f"  resets at: {info.get('budget_reset_at')}")
    print(f"  models:    {info.get('models')}  rpm: {info.get('rpm_limit')}  tpm: {info.get('tpm_limit')}")
    return 0


def _end_user(args: argparse.Namespace) -> int:
    print(end_user_id(args.tenant, args.principal) if args.principal else legacy_end_user_id(args.tenant))
    return 0


def _read_person_budget(client: httpx.Client) -> tuple[dict | None, httpx.Response]:
    """The default per-person budget row (None when there is none) and the raw answer, so a caller can report a refusal."""
    response = client.post("/budget/info", json={"budgets": [PERSON_BUDGET_ID]})
    rows = response.json() if response.status_code == 200 else []
    return (rows[0] if rows else None), response


def _person_budget_set(args: argparse.Namespace) -> int:
    if args.max_budget <= 0:
        raise SystemExit("error: --max-budget must be above 0 (a cap of 0 would refuse every person's second call)")
    body = {"budget_id": PERSON_BUDGET_ID, "max_budget": args.max_budget, "budget_duration": args.budget_duration}
    with _client() as client:
        existing, lookup = _read_person_budget(client)
        if lookup.status_code != 200:
            print(f"error: LiteLLM refused to look the budget up (HTTP {lookup.status_code}): {lookup.text[:300]}", file=sys.stderr)
            return 1
        # /budget/new creates a row and fails on a duplicate id, so an existing budget is updated, not re-created.
        response = client.post("/budget/update" if existing else "/budget/new", json=body)
    if response.status_code != 200:
        print(f"error: LiteLLM refused to save the budget (HTTP {response.status_code}): {response.text[:300]}", file=sys.stderr)
        return 1
    print(f"{'Updated' if existing else 'Created'} budget {PERSON_BUDGET_ID!r}: ${args.max_budget:g} per {args.budget_duration} for every person without a budget of its own.")
    print("It applies through litellm_settings.max_end_user_budget_id (both shipped configs name it), caps each PERSON (not a tenant), covers chat calls only, and a change takes up to 60 s.")
    return 0


def _person_budget_info(args: argparse.Namespace) -> int:
    with _client() as client:
        budget, response = _read_person_budget(client)
    if response.status_code != 200:
        print(f"error: LiteLLM refused (HTTP {response.status_code}): {response.text[:300]}", file=sys.stderr)
        return 1
    if budget is None:
        print(f"Budget {PERSON_BUDGET_ID!r}: NOT FOUND.", file=sys.stderr)
        print("LiteLLM finds no default per-person budget, so it applies none (and logs 'Default end user budget not found in database' on every call).", file=sys.stderr)
        print("Create it: python -m scripts.litellm_key person-budget set --max-budget <usd per person per window>", file=sys.stderr)
        return 1
    max_budget = budget.get("max_budget")
    print(f"Budget {PERSON_BUDGET_ID!r}")
    print(f"  cap:       ${max_budget:g} per person per {budget.get('budget_duration')}" if isinstance(max_budget, int | float) else "  cap:       NONE (a budget with no max_budget caps nothing)")
    print(f"  resets at: {budget.get('budget_reset_at')}")
    return 0


def _person_budget(args: argparse.Namespace) -> int:
    return {"set": _person_budget_set, "info": _person_budget_info}[args.action](args)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return {"create": _create, "info": _info, "end-user": _end_user, "person-budget": _person_budget}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
