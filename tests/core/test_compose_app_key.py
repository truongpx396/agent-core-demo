"""The prod compose file hands the app a SCOPED gateway key, not the gateway's master key.

The master key is LiteLLM admin: it mints keys, reads every spend log, rewrites models, removes
budgets — and cannot carry a budget itself. An app holding it has no gateway-side spend cap and
puts the whole gateway one container compromise away. `scripts/litellm_key.py` mints the scoped key;
this pins the wiring, which is easy to undo with a one-line edit and invisible until an incident.

Static, reading the YAML (no Docker). The `${A:-${B}}` substitution itself was checked against a real
`docker compose config` when this was written: with `LITELLM_APP_KEY` set the app services receive it,
with it unset OR empty they fall back to the master key, and only `litellm` is ever given the master
key as its own `LITELLM_MASTER_KEY`.
"""
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "docker-compose.prod.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_the_app_prefers_the_scoped_key_and_only_falls_back_to_the_master_one():
    api_key = _compose()["x-app-env"]["OPENAI_API_KEY"]

    assert api_key.startswith("${LITELLM_APP_KEY"), "the scoped key must come first"
    assert "LITELLM_MASTER_KEY" in api_key, "falling back keeps an existing deployment running"


def test_no_service_but_litellm_is_given_the_master_key_by_name():
    """Via the shared anchor the app only ever sees the scoped key (or the explicit fallback above);
    a service with its own `LITELLM_MASTER_KEY` variable would be a second, unreviewed route."""
    holders = [
        name
        for name, service in _compose()["services"].items()
        if "LITELLM_MASTER_KEY" in (service.get("environment") or {})
    ]

    assert holders == ["litellm"]


def test_the_prod_env_template_documents_the_app_key():
    template = (COMPOSE.parents[1] / "env" / "prod.env.example").read_text()

    assert "\nLITELLM_APP_KEY=" in template
