"""The `chat-strong` escalation alias in both gateway configs (specs/014 contracts/gateway-aliases.md section 1).

The alias must ALWAYS exist so the proxy starts and the app's `STRONG_CHAT_MODEL=chat-strong` always resolves; it must be inert until the app
asks for it; and in production an unset variable must make it equal to `chat` rather than stop the proxy. No gateway `fallbacks` entry may
name it: the degrade-to-local is the app's, so that the usage ledger prices the model that answered instead of the one that did not.
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "deploy/litellm/litellm-config.yaml"
PROD = REPO / "deploy/litellm/litellm-config.prod.yaml"


def _config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _entry(path: Path, alias: str) -> dict:
    (entry,) = [m for m in _config(path)["model_list"] if m["model_name"] == alias]
    return entry["litellm_params"]


def test_both_configs_parse_and_define_the_alias():
    for path in (DEV, PROD):
        assert "chat-strong" in {m["model_name"] for m in _config(path)["model_list"]}, path.name


def test_in_dev_the_alias_is_a_local_ollama_model_so_the_seam_needs_no_third_party_or_key():
    params = _entry(DEV, "chat-strong")

    assert params["model"].startswith("ollama_chat/")  # the native-tool-calling provider, like `chat` (the plain `ollama/` one loops on tools)
    assert params["api_base"] == _entry(DEV, "chat")["api_base"] and "api_key" not in params
    assert params["num_ctx"] == _entry(DEV, "chat")["num_ctx"]  # the history ceilings are sized against this context window


def test_the_dev_alias_is_a_bigger_model_than_chat_otherwise_escalating_would_change_nothing():
    assert _entry(DEV, "chat-strong")["model"] != _entry(DEV, "chat")["model"]


def test_in_prod_the_alias_reads_three_variables_and_nothing_is_hard_coded():
    params = _entry(PROD, "chat-strong")

    assert params == {
        "model": "os.environ/LLM_STRONG_CHAT_MODEL",
        "api_base": "os.environ/LLM_STRONG_API_BASE",
        "api_key": "os.environ/LLM_STRONG_API_KEY",
    }


def test_no_gateway_fallback_names_the_strong_alias_in_either_config():
    for path in (DEV, PROD):
        settings = _config(path).get("router_settings", {}) | _config(path).get("litellm_settings", {})
        for rule in settings.get("fallbacks", []):
            for source, targets in rule.items():
                assert "chat-strong" not in {source, *targets}, f"{path.name}: a gateway fallback would price a local answer as the strong alias"


def test_the_existing_chat_fallback_is_unchanged():
    assert {"chat": ["chat-backup"]} in (_config(DEV).get("litellm_settings", {}).get("fallbacks", []))


def test_prod_compose_defaults_each_strong_variable_to_the_chat_value_so_an_unset_one_cannot_stop_the_proxy():
    compose = yaml.safe_load((REPO / "deploy/compose/docker-compose.prod.yml").read_text())
    env = compose["services"]["litellm"]["environment"]

    assert env["LLM_STRONG_CHAT_MODEL"] == "${LLM_STRONG_CHAT_MODEL:-${LLM_CHAT_MODEL}}"
    assert env["LLM_STRONG_API_BASE"] == "${LLM_STRONG_API_BASE:-${LLM_API_BASE}}"
    assert env["LLM_STRONG_API_KEY"] == "${LLM_STRONG_API_KEY:-${LLM_API_KEY}}"


def test_the_prod_env_example_documents_the_three_variables_as_optional():
    text = (REPO / "deploy/env/prod.env.example").read_text()

    for name in ("LLM_STRONG_CHAT_MODEL", "LLM_STRONG_API_BASE", "LLM_STRONG_API_KEY"):
        assert f"# {name}=" in text, f"{name} must be listed, commented out (leaving it unset means the same as chat)"
