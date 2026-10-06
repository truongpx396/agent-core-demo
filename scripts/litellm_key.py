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

    # the end-user id the gateway shows for a tenant (the app sends a hash, not the name)
    python -m scripts.litellm_key end-user --tenant acme

Sizing `--max-budget`: it is the LAST line of defence, so set it ABOVE what the app's own limits can
legitimately add up to (the sum of your tenants' monthly caps), so it fires only when they have
failed. A budget below normal use turns routine traffic into an outage.

The key is printed once, to stdout, and is never logged; put it in LITELLM_APP_KEY.
"""
import argparse
import os
import sys

import httpx

from app.agent.gateway import end_user_id
from app.agent.model_resolver import admin_base_url

DEFAULT_MODELS = "chat,embed"


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
    create.add_argument("--models", default=DEFAULT_MODELS, help=f"comma-separated aliases the key may call (default {DEFAULT_MODELS}, the two that deploy/litellm/litellm-config.prod.yaml defines); list every alias the app calls")
    create.add_argument("--alias", default="agent-core-app", help="a name to find the key by in LiteLLM's UI")

    sub.add_parser("info", help="spend, budget and reset time of the key in LITELLM_APP_KEY")

    end_user = sub.add_parser("end-user", help="the end-user id the gateway shows for a tenant")
    end_user.add_argument("--tenant", required=True)
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
    print(end_user_id(args.tenant))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return {"create": _create, "info": _info, "end-user": _end_user}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
