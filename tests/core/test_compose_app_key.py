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


# --- the dev compose file: the same rule, which it did not follow ---------------------------------------------------------------------
#
# A container does not read `.env` (compose uses it only to fill in `${...}`), and the dev file's shared `x-app-env` block had no
# OPENAI_API_KEY, so every containerized role sent the app's built-in default, the dev master key, whatever the operator had minted and
# written in `.env`. A scoped key's budget never moved and the spend log named `default_user_id` as the caller. Found when a minted key
# showed spend 0 and no last-active time after it was put in `.env`. The `${A:-B}` substitution was checked with a real
# `docker compose config`: LITELLM_APP_KEY set -> every app role gets it; unset or empty -> `sk-anything`.

DEV_COMPOSE = COMPOSE.parent / "docker-compose.yml"
APP_ROLES = ["api", "agent-worker", "agent-worker-support", "agent-worker-ops", "agent-worker-sales", "ingest-worker"]


def _dev_compose() -> dict:
    return yaml.safe_load(DEV_COMPOSE.read_text())


def test_in_dev_the_shared_app_env_prefers_the_scoped_key_and_falls_back_to_the_dev_master_key():
    api_key = _dev_compose()["x-app-env"]["OPENAI_API_KEY"]

    assert api_key == "${LITELLM_APP_KEY:-sk-anything}"


def test_in_dev_the_fallback_is_the_master_key_the_litellm_service_really_runs_with():
    """A fallback that is not the proxy's own key would break every containerized role the moment the variable is unset."""
    compose = _dev_compose()
    fallback = compose["x-app-env"]["OPENAI_API_KEY"].removeprefix("${LITELLM_APP_KEY:-").removesuffix("}")

    assert fallback == compose["services"]["litellm"]["environment"]["LITELLM_MASTER_KEY"]


def test_in_dev_every_containerized_app_role_gets_the_key():
    """Through the shared anchor, directly or merged: a role that spelled out its own environment would silently keep the old behaviour."""
    services = _dev_compose()["services"]

    for role in APP_ROLES:
        assert services[role]["environment"].get("OPENAI_API_KEY") == "${LITELLM_APP_KEY:-sk-anything}", role


def test_in_dev_no_service_but_litellm_is_given_the_master_key_by_name():
    holders = [name for name, service in _dev_compose()["services"].items() if "LITELLM_MASTER_KEY" in (service.get("environment") or {})]

    assert holders == ["litellm"]


def test_the_dev_env_example_names_the_container_variable_and_never_holds_a_key():
    """Commented and empty, like the prod template's secret: a real key never belongs in an example file."""
    text = (DEV_COMPOSE.parents[2] / ".env.example").read_text()

    assert "\n# LITELLM_APP_KEY=\n" in text
