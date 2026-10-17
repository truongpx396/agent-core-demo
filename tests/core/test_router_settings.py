"""The routing settings (app/core/config.py): inert by default, and a configuration that could never work is refused at start-up.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings

ROUTER_VARIABLES = ("STRONG_CHAT_MODEL", "ROUTER_ENABLED", "ROUTER_THRESHOLD", "ROUTER_TIMEOUT_SECONDS", "CHAT_MODEL")


@pytest.fixture(autouse=True)
def _scrubbed_environment(monkeypatch):
    for name in ROUTER_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_the_router_is_off_and_names_no_strong_model_by_default():
    settings = Settings(_env_file=None)

    assert (settings.strong_chat_model, settings.router_enabled) == ("", False)
    assert (settings.router_threshold, settings.router_timeout_seconds) == (0.5, 0.5)


def test_enabling_the_router_without_a_strong_alias_refuses_to_start_and_names_both_variables():
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, router_enabled=True)

    assert "ROUTER_ENABLED" in str(refused.value) and "STRONG_CHAT_MODEL" in str(refused.value)


def test_a_strong_alias_equal_to_the_chat_model_is_refused():
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, chat_model="chat", strong_chat_model="chat")

    assert "STRONG_CHAT_MODEL must differ from CHAT_MODEL" in str(refused.value)


def test_naming_a_strong_alias_while_the_router_is_off_is_allowed():
    # the alias can be configured ahead of the switch; nothing is asked of the classifier until the switch is on
    settings = Settings(_env_file=None, strong_chat_model="chat-strong")

    assert settings.strong_chat_model == "chat-strong" and settings.router_enabled is False


def test_a_valid_router_configuration_is_accepted():
    settings = Settings(_env_file=None, strong_chat_model="chat-strong", router_enabled=True, router_threshold=0.8)

    assert settings.router_enabled is True and settings.router_threshold == 0.8


@pytest.mark.parametrize("threshold", [-0.01, 1.01, 5])
def test_a_threshold_outside_zero_to_one_is_rejected(threshold):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, router_threshold=threshold)


@pytest.mark.parametrize("threshold", [0.0, 1.0])
def test_the_threshold_bounds_themselves_are_allowed(threshold):
    assert Settings(_env_file=None, router_threshold=threshold).router_threshold == threshold


@pytest.mark.parametrize("seconds", [0, -1, 5.01, 60])
def test_a_timeout_that_is_not_a_short_positive_deadline_is_rejected(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, router_timeout_seconds=seconds)


def test_a_refusal_never_prints_a_secret():
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, router_enabled=True, openai_api_key="sk-this-must-not-be-printed", litellm_master_key="sk-nor-this")

    assert "sk-this-must-not-be-printed" not in str(refused.value) and "sk-nor-this" not in str(refused.value)
