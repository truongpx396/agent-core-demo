"""The routing settings (app/core/config.py): inert by default, and a configuration that could never work is refused at start-up (specs/015 FR-021).

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings

ROUTER_VARIABLES = (
    "ROUTER_MODE", "ROUTER_ENABLED", "ROUTER_THRESHOLD", "ROUTER_MAX_TIER", "ROUTER_LABELS", "ROUTER_LONG_PROMPT_CHARS", "ROUTER_TIMEOUT_SECONDS",
    "MEDIUM_CHAT_MODEL", "STRONG_CHAT_MODEL", "CHAT_MODEL",
)


@pytest.fixture(autouse=True)
def _scrubbed_environment(monkeypatch):
    for name in ROUTER_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_router_is_off_and_names_no_routed_model_by_default():
    settings = Settings(_env_file=None)

    assert (settings.router_mode, settings.medium_chat_model, settings.strong_chat_model) == ("off", "", "")
    assert (settings.router_max_tier, settings.router_labels) == ("strong", True)
    assert (settings.router_long_prompt_chars, settings.router_timeout_seconds) == (1500, 0.5)


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_a_router_that_can_route_nowhere_refuses_to_start_and_names_the_variables(mode):
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, router_mode=mode)

    message = str(refused.value)
    assert "ROUTER_MODE" in message and "MEDIUM_CHAT_MODEL" in message and "STRONG_CHAT_MODEL" in message


def test_off_needs_no_alias_and_naming_aliases_ahead_of_the_switch_is_allowed():
    settings = Settings(_env_file=None, medium_chat_model="chat-medium", strong_chat_model="chat-strong")

    assert settings.router_mode == "off" and (settings.medium_chat_model, settings.strong_chat_model) == ("chat-medium", "chat-strong")


@pytest.mark.parametrize("kwargs", [{"medium_chat_model": "chat-medium"}, {"strong_chat_model": "chat-strong"},
                                    {"medium_chat_model": "chat-medium", "strong_chat_model": "chat-strong"}])
@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_one_routed_alias_is_enough_for_shadow_or_on(mode, kwargs):
    assert Settings(_env_file=None, router_mode=mode, **kwargs).router_mode == mode


@pytest.mark.parametrize("kwargs", [
    {"chat_model": "chat", "strong_chat_model": "chat"},
    {"chat_model": "chat", "medium_chat_model": "chat"},
    {"medium_chat_model": "same", "strong_chat_model": "same"},
])
def test_two_tiers_sharing_an_alias_is_refused(kwargs):
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, **kwargs)

    assert "three different gateway aliases" in str(refused.value)


def test_a_mode_other_than_the_three_is_rejected():
    for mode in ("true", "enabled", "ON", ""):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, router_mode=mode)


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_a_leftover_router_enabled_that_is_true_is_refused_and_names_its_replacement(monkeypatch, value):
    monkeypatch.setenv("ROUTER_ENABLED", value)

    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)

    assert "ROUTER_ENABLED was replaced by ROUTER_MODE" in str(refused.value)


@pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
def test_a_leftover_router_enabled_that_is_false_changes_nothing_and_is_ignored(monkeypatch, value):
    monkeypatch.setenv("ROUTER_ENABLED", value)
    monkeypatch.setenv("ROUTER_THRESHOLD", "0.8")  # meant nothing without the switch

    assert Settings(_env_file=None).router_mode == "off"


@pytest.mark.parametrize("tier", ["local", "medium", "strong"])
def test_the_ceiling_accepts_the_three_tiers(tier):
    assert Settings(_env_file=None, router_max_tier=tier).router_max_tier == tier


@pytest.mark.parametrize("tier", ["", "weak", "high", "STRONG"])
def test_a_ceiling_that_is_not_a_tier_is_rejected(tier):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, router_max_tier=tier)


@pytest.mark.parametrize("chars", [0, 199, -1])
def test_a_long_prompt_threshold_below_two_hundred_is_rejected(chars):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, router_long_prompt_chars=chars)


def test_two_hundred_characters_is_the_lowest_allowed_threshold():
    assert Settings(_env_file=None, router_long_prompt_chars=200).router_long_prompt_chars == 200


def test_an_empty_label_set_means_rules_only_and_is_allowed():
    assert Settings(_env_file=None, router_labels=False).router_labels is False


@pytest.mark.parametrize("seconds", [0, -1, 5.01, 60])
def test_a_timeout_that_is_not_a_short_positive_deadline_is_rejected(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, router_timeout_seconds=seconds)


def test_a_refusal_never_prints_a_secret():
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, router_mode="on", openai_api_key="sk-this-must-not-be-printed", litellm_master_key="sk-nor-this")

    assert "sk-this-must-not-be-printed" not in str(refused.value) and "sk-nor-this" not in str(refused.value)


def test_a_refusal_for_a_shared_alias_does_not_print_the_alias():
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, medium_chat_model="an-alias-name-4711", strong_chat_model="an-alias-name-4711")

    assert "an-alias-name-4711" not in str(refused.value)
