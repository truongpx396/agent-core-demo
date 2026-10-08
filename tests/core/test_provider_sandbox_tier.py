"""The provider-sandbox tier's own safety rails (tests/provider_sandbox/, specs/010 T029a).

That tier talks to a real payment provider's sandbox over the internet with the operator's own key and creates objects there. What keeps
that safe is not exercised by the tier itself (it skips without a key), so it is pinned here, hermetically:

  * the default run and CI never select it, so no pull request depends on, or spends, a third party's API;
  * every test in it carries the marker, so a test added there cannot slip into the default run;
  * a LIVE-mode (or any non-test) key, or a Polar deployment configured for production, makes it fail before a request is made, never
    skip and never create;
  * it will not run beside someone's own `stripe listen` / `polar listen`, whose app would receive the tier's sample events too;
  * what it prints from the provider CLIs, which includes the webhook signing secrets, is redacted.
"""
import ast
import tomllib
from pathlib import Path

import pytest

from tests.provider_sandbox import conftest as tier
from tests.provider_sandbox.conftest import require_polar_sandbox, require_test_mode_key
from tests.provider_sandbox.receiver import redact

ROOT = Path(__file__).resolve().parents[2]
TIER = ROOT / "tests" / "provider_sandbox"


def test_the_default_run_excludes_the_tier():
    addopts = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["addopts"]

    assert "not provider_sandbox" in addopts


def test_the_marker_is_registered_so_a_typo_cannot_silently_unmark_a_test():
    markers = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["markers"]

    assert any(marker.startswith("provider_sandbox:") for marker in markers)


def test_no_ci_job_selects_the_tier():
    """CI selects tiers with `-m`; the tier must never appear in any of them. A secret-less CI run would only skip it, but a CI run WITH a
    key would create objects in someone's sandbox on every push."""
    assert "provider_sandbox" not in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


@pytest.mark.parametrize("path", sorted(TIER.glob("test_*.py")), ids=lambda p: p.name)
def test_every_test_module_in_the_tier_is_marked(path):
    assignments = [
        node for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "pytestmark" for t in node.targets)
    ]

    assert assignments and "provider_sandbox" in ast.unparse(assignments[0].value)


def test_the_tier_has_at_least_one_test_module():
    assert list(TIER.glob("test_*.py")), "the marker test above would pass vacuously on an empty directory"


def outcome(key: str | None) -> str:
    """What the guard did with `key`, as a word. `pytest.raises(pytest.fail.Exception)` is not enough: a `Skipped` raised inside a test
    makes pytest report the TEST as skipped, which is not a failure, so a guard that skipped a live key would pass such a test."""
    try:
        require_test_mode_key(key)
    except pytest.skip.Exception:
        return "skipped"
    except pytest.fail.Exception:
        return "failed"
    return "accepted"


class TestTheKeyGuard:
    @pytest.mark.parametrize("key", [None, ""])
    def test_no_key_is_a_skip_not_a_failure(self, key):
        assert outcome(key) == "skipped"

    @pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "pk_test_abc", "pk_live_abc", "whsec_abc", "garbage", " sk_test_abc"])
    def test_anything_that_is_not_a_test_mode_secret_or_restricted_key_fails_before_a_request(self, key):
        """FAILS, not skips: a skip would let someone believe the check had run."""
        assert outcome(key) == "failed"

    @pytest.mark.parametrize("key", ["sk_test_abc", "rk_test_abc"])
    def test_a_test_mode_key_is_accepted_and_returned_unchanged(self, key):
        assert outcome(key) == "accepted" and require_test_mode_key(key) == key


class TestRedaction:
    # Key-shaped values are assembled at run time: a literal one in the source is exactly what a secret scanner (CI's Trivy gate) looks for,
    # and a scanner cannot tell a fixture from a leak.
    BODY = "AbC123xyz789"

    def test_a_signing_secret_is_not_shown(self):
        secret = "whsec" + "_" + "0123456789abcdef0123456789abcdef"

        shown = redact(f"Ready! Your webhook signing secret is {secret} (^C to quit)")

        assert "0123456789abcdef" not in shown and "whsec_***" in shown

    @pytest.mark.parametrize("prefix", ["sk_test_", "rk_test_", "sk_live_"])
    def test_an_api_key_is_not_shown(self, prefix):
        shown = redact(f"using {prefix + self.BODY} for this run")

        assert self.BODY not in shown and prefix in shown


class TestPolarEnvironmentGuard:
    """Polar tokens look the same in sandbox and production, so the environment setting is the only thing that says which this is."""

    @staticmethod
    def polar_outcome(token, environment) -> str:
        try:
            require_polar_sandbox(token, environment)
        except pytest.skip.Exception:
            return "skipped"
        except pytest.fail.Exception:
            return "failed"
        return "accepted"

    @pytest.mark.parametrize("token", [None, ""])
    def test_no_token_is_a_skip_not_a_failure(self, token):
        assert self.polar_outcome(token, "sandbox") == "skipped"

    @pytest.mark.parametrize("environment", ["production", "", "staging", "SANDBOX"])
    def test_anything_but_the_sandbox_environment_fails_before_a_request_even_with_a_token(self, environment):
        """FAILS, not skips: a skip would let someone believe the check had run."""
        assert self.polar_outcome("polar_oat_x", environment) == "failed"

    def test_a_missing_token_is_a_skip_even_in_production_because_nothing_would_run(self):
        assert self.polar_outcome(None, "production") == "skipped"

    def test_a_token_in_the_sandbox_environment_is_accepted_and_returned_unchanged(self):
        assert self.polar_outcome("polar_oat_x", "sandbox") == "accepted"
        assert require_polar_sandbox("polar_oat_x", "sandbox") == "polar_oat_x"

    def test_the_api_this_tier_talks_to_is_fixed_to_the_sandbox_and_not_read_from_a_setting(self):
        assert tier.POLAR_SANDBOX_API == "https://sandbox-api.polar.sh/v1"


class TestItWillNotRunBesideSomeoneElsesListener:
    @staticmethod
    def pgrep_says(monkeypatch, *pids: str):
        class Result:
            stdout = "\n".join(pids)

        monkeypatch.setattr(tier.subprocess, "run", lambda *a, **k: Result())

    def test_another_listening_process_is_found(self, monkeypatch):
        self.pgrep_says(monkeypatch, "88727")

        assert tier.other_listener("polar") == "88727"

    def test_none_is_found_when_none_is_running(self, monkeypatch):
        self.pgrep_says(monkeypatch)

        assert tier.other_listener("polar") is None

    def test_this_process_is_never_counted_as_another(self, monkeypatch):
        self.pgrep_says(monkeypatch, str(tier.os.getpid()))

        assert tier.other_listener("stripe") is None

    def test_the_search_is_for_the_clis_listen_command(self, monkeypatch):
        seen = []

        class Result:
            stdout = ""

        monkeypatch.setattr(tier.subprocess, "run", lambda command, **k: seen.append(command) or Result())
        tier.other_listener("polar")

        assert seen == [["pgrep", "-f", "polar listen"]]

    def test_a_foreign_listener_skips_the_tier_and_says_how_to_proceed(self, monkeypatch):
        self.pgrep_says(monkeypatch, "88727")
        monkeypatch.delenv(tier.SHARE_LISTENER, raising=False)

        with pytest.raises(pytest.skip.Exception) as caught:
            tier.refuse_beside_a_foreign_listener("polar")

        assert "88727" in str(caught.value) and tier.SHARE_LISTENER in str(caught.value)

    def test_the_operator_can_accept_it_explicitly(self, monkeypatch):
        self.pgrep_says(monkeypatch, "88727")
        monkeypatch.setenv(tier.SHARE_LISTENER, "1")

        tier.refuse_beside_a_foreign_listener("polar")  # does not skip

    @pytest.mark.parametrize("value", ["", "0", "true", "yes", "2"])
    def test_only_a_literal_one_accepts_it(self, monkeypatch, value):
        self.pgrep_says(monkeypatch, "88727")
        monkeypatch.setenv(tier.SHARE_LISTENER, value)

        with pytest.raises(pytest.skip.Exception):
            tier.refuse_beside_a_foreign_listener("polar")

    def test_with_no_other_listener_it_runs(self, monkeypatch):
        self.pgrep_says(monkeypatch)
        monkeypatch.delenv(tier.SHARE_LISTENER, raising=False)

        tier.refuse_beside_a_foreign_listener("stripe")  # does not skip


class TestPolarRedaction:
    BODY = "AbC123xyz789"

    @pytest.mark.parametrize("prefix", ["polar_oat_", "polar_pat_"])
    def test_a_polar_token_is_not_shown(self, prefix):
        shown = redact(f"using {prefix + self.BODY} for this run")

        assert self.BODY not in shown and prefix in shown

    def test_a_secret_with_no_recognizable_shape_is_redacted_by_value(self):
        """Polar's CLI prints a bare 32-character secret that no pattern could know; the listener passes it in."""
        secret = "0123456789abcdef0123456789abcdef"

        assert secret not in redact(f"Secret      {secret}  use this to verify signatures locally", [secret])

    def test_an_empty_known_value_does_not_blank_the_whole_text(self):
        assert redact("hello", [""]) == "hello"
