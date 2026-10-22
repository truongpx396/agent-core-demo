"""The three chat tiers in both gateway configs: easy `chat`, medium `chat-medium`, strong `chat-strong`.

`chat` and `chat-strong` are specs/014's (tests/core/test_gateway_aliases_config.py pins `chat-strong`); `chat-medium` is the third and follows
the same rules, because the point of an alias is that an operator can attach a hosted model by editing one entry:

  * it ALWAYS exists, so the proxy starts and the alias resolves, and in dev it is a local stand-in, so the stack needs no third party and no key;
  * in production an unset variable makes it equal to `chat` rather than stop the proxy;
  * no gateway `fallbacks` entry names it: a fallback would answer with one model and price the call as another (the ledger prices the alias the
    app asked for, `research.md` C3), and the tier is exactly the thing whose price differs.

Static, reading YAML (no Docker, no LiteLLM).
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "deploy/litellm/litellm-config.yaml"
PROD = REPO / "deploy/litellm/litellm-config.prod.yaml"
TIERS = ("chat", "chat-medium", "chat-strong")


def _config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _entry(path: Path, alias: str) -> dict:
    (entry,) = [m for m in _config(path)["model_list"] if m["model_name"] == alias]
    return entry["litellm_params"]


def test_both_configs_define_all_three_tiers():
    for path in (DEV, PROD):
        assert set(TIERS) <= {m["model_name"] for m in _config(path)["model_list"]}, path.name


def test_in_dev_the_medium_alias_is_a_local_ollama_model_so_the_stack_needs_no_third_party_or_key():
    params = _entry(DEV, "chat-medium")

    assert params["model"].startswith("ollama_chat/")  # the native-tool-calling provider, like `chat` (the plain `ollama/` one loops on tools)
    assert params["api_base"] == _entry(DEV, "chat")["api_base"] and "api_key" not in params
    assert params["num_ctx"] == _entry(DEV, "chat")["num_ctx"]  # the history ceilings are sized against this context window


def test_in_prod_the_medium_alias_reads_three_variables_and_nothing_is_hard_coded():
    assert _entry(PROD, "chat-medium") == {
        "model": "os.environ/LLM_MEDIUM_CHAT_MODEL",
        "api_base": "os.environ/LLM_MEDIUM_API_BASE",
        "api_key": "os.environ/LLM_MEDIUM_API_KEY",
    }


def test_no_gateway_fallback_names_the_medium_alias_in_either_config():
    for path in (DEV, PROD):
        settings = _config(path).get("router_settings", {}) | _config(path).get("litellm_settings", {})
        for rule in settings.get("fallbacks", []):
            for source, targets in rule.items():
                assert "chat-medium" not in {source, *targets}, f"{path.name}: a gateway fallback would price a call as a model that did not answer it"


def test_prod_compose_defaults_each_medium_variable_to_the_chat_value_so_an_unset_one_cannot_stop_the_proxy():
    compose = yaml.safe_load((REPO / "deploy/compose/docker-compose.prod.yml").read_text())
    env = compose["services"]["litellm"]["environment"]

    assert env["LLM_MEDIUM_CHAT_MODEL"] == "${LLM_MEDIUM_CHAT_MODEL:-${LLM_CHAT_MODEL}}"
    assert env["LLM_MEDIUM_API_BASE"] == "${LLM_MEDIUM_API_BASE:-${LLM_API_BASE}}"
    assert env["LLM_MEDIUM_API_KEY"] == "${LLM_MEDIUM_API_KEY:-${LLM_API_KEY}}"


def test_the_prod_env_example_documents_the_three_medium_variables_as_optional():
    text = (REPO / "deploy/env/prod.env.example").read_text()

    for name in ("LLM_MEDIUM_CHAT_MODEL", "LLM_MEDIUM_API_BASE", "LLM_MEDIUM_API_KEY"):
        assert f"# {name}=" in text, f"{name} must be listed, commented out (leaving it unset means the same as chat)"


def test_dev_compose_passes_the_hosted_tier_keys_through_empty_so_setting_one_later_is_only_an_env_change():
    compose = yaml.safe_load((REPO / "deploy/compose/docker-compose.yml").read_text())
    env = compose["services"]["litellm"]["environment"]

    assert env["GEMINI_API_KEY_MEDIUM"] == "${GEMINI_API_KEY_MEDIUM:-}"
    assert env["GEMINI_API_KEY_STRONG"] == "${GEMINI_API_KEY_STRONG:-}"


def test_the_dev_env_example_lists_the_hosted_tier_keys_commented_out_and_never_with_a_value():
    text = (REPO / ".env.example").read_text()

    for name in ("GEMINI_API_KEY_MEDIUM", "GEMINI_API_KEY_STRONG"):
        assert f"# {name}=\n" in text, f"{name} must be listed, commented out and empty: a real key never belongs in an example file"
