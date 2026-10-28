"""How the API is told who may call it (specs/018, task T055): `AUTH_MODE`, the front door's key address, and the refusal to start without it.

The default is the safe one. `identity` mode trusts only the signed identity the front door adds to a verified request; `headers` mode trusts
two headers any client can set, so it is something a person chooses (the hermetic tests, and `.env.example` for running the API alone), never
something an unconfigured deployment falls into. An API that cannot verify anyone must not come up, so the check runs when it STARTS and names
the variable, not on the first request and not by value (a URL can carry a credential).

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`, which sets `AUTH_MODE=headers`.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.internal_identity import AuthConfigError, check_settings

REPO = Path(__file__).resolve().parents[2]
_ENV = ("AUTH_MODE", "AUTHLIMIT_KEYS_URL", "AUTHLIMIT_KEYS_CACHE_SECONDS", "AUTHLIMIT_KEYS_STALE_GRACE_SECONDS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _example(path: str) -> dict[str, str]:
    """The NAME=value lines of an env example (a commented line is not a setting)."""
    out = {}
    for line in (REPO / path).read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip()
    return out


# --- the settings -----------------------------------------------------------------------------------------------------------------------


def test_the_default_is_identity_mode_with_no_key_address():
    s = _settings()

    assert (s.auth_mode, s.authlimit_keys_url) == ("identity", "")
    assert (s.authlimit_keys_cache_seconds, s.authlimit_keys_stale_grace_seconds) == (60, 600)


def test_headers_mode_is_chosen_by_name_and_only_by_name(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "headers")

    assert _settings().auth_mode == "headers"


@pytest.mark.parametrize("value", ["", "header", "HEADERS", "none", "off", "disabled", "true"])
def test_anything_else_is_refused_not_read_as_a_mode(value):
    """A typo must not quietly fall back to either mode: one of them trusts any client."""
    with pytest.raises(ValidationError) as caught:
        _settings(auth_mode=value)

    assert "auth_mode" in str(caught.value)


@pytest.mark.parametrize("field,bad", [
    ("authlimit_keys_cache_seconds", 9),
    ("authlimit_keys_cache_seconds", 3601),
    ("authlimit_keys_stale_grace_seconds", -1),
    ("authlimit_keys_stale_grace_seconds", 3601),
])
def test_the_key_timings_are_bounded(field, bad):
    with pytest.raises(ValidationError):
        _settings(**{field: bad})


def test_a_grace_of_zero_is_allowed_and_means_no_grace():
    assert _settings(authlimit_keys_stale_grace_seconds=0).authlimit_keys_stale_grace_seconds == 0


# --- refusing to start ------------------------------------------------------------------------------------------------------------------


def test_identity_mode_without_the_key_address_does_not_start_and_names_the_variable():
    with pytest.raises(AuthConfigError, match="AUTHLIMIT_KEYS_URL"):
        check_settings(_settings())


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_address_is_no_address(blank):
    with pytest.raises(AuthConfigError, match="AUTHLIMIT_KEYS_URL"):
        check_settings(_settings(authlimit_keys_url=blank))


def test_header_mode_needs_no_key_address():
    check_settings(_settings(auth_mode="headers"))


@pytest.mark.parametrize("url", [
    "http://authlimit:8080/internal/jwks.json",       # a compose service name: no dot, an internal network
    "http://localhost:8080/internal/jwks.json",
    "http://127.0.0.1:8080/internal/jwks.json",
    "http://[::1]:8080/internal/jwks.json",
    "http://authlimit.localhost:8080/internal/jwks.json",
    "https://auth.example.com/internal/jwks.json",
    "  https://auth.example.com/internal/jwks.json  ",
])
def test_the_addresses_an_operator_really_uses_are_accepted(url):
    check_settings(_settings(authlimit_keys_url=url))


@pytest.mark.parametrize("url", [
    "authlimit:8080/internal/jwks.json",              # no scheme
    "ftp://authlimit/internal/jwks.json",
    "file:///etc/passwd",
    "http://",
    "http://auth.example.com/internal/jwks.json",     # plain http to a name that crosses networks: a person in the middle hands over their own keys
    "http://10.0.0.5:8080/internal/jwks.json",        # a private address is still not loopback
    "https://user:hunter2@auth.example.com/jwks.json",
    "https://auth.example.com/jwks.json?token=abc",
    "https://auth.example.com/jwks.json#frag",
])
def test_an_address_that_could_hand_the_api_someone_elses_keys_is_refused(url):
    with pytest.raises(AuthConfigError, match="AUTHLIMIT_KEYS_URL"):
        check_settings(_settings(authlimit_keys_url=url))


def test_the_message_never_echoes_the_address_because_an_address_can_carry_a_credential():
    with pytest.raises(AuthConfigError) as caught:
        check_settings(_settings(authlimit_keys_url="https://user:hunter2@auth.example.com/jwks.json"))

    assert "hunter2" not in str(caught.value) and "user:" not in str(caught.value)


# --- the examples an operator copies ----------------------------------------------------------------------------------------------------


def test_the_example_for_running_the_api_alone_chooses_header_mode_explicitly():
    """A developer who copies `.env.example` and runs `make serve` has no front door, so the copy must say so (spec assumption A9)."""
    assert _example(".env.example")["AUTH_MODE"] == "headers"


def test_the_production_example_is_identity_mode_with_an_address_that_passes_the_startup_check():
    values = _example("deploy/env/prod.env.example")

    assert values["AUTH_MODE"] == "identity"
    check_settings(SimpleNamespace(auth_mode=values["AUTH_MODE"], authlimit_keys_url=values["AUTHLIMIT_KEYS_URL"]))
