"""The provider-sandbox tier's own safety rails (tests/provider_sandbox/, specs/010 T029a).

That tier talks to a real payment provider's sandbox over the internet with the operator's own key and creates objects there. What keeps
that safe is not exercised by the tier itself (it skips without a key), so it is pinned here, hermetically:

  * the default run and CI never select it, so no pull request depends on, or spends, a third party's API;
  * every test in it carries the marker, so a test added there cannot slip into the default run;
  * a LIVE-mode (or any non-test) key makes it fail before a request is made, never skip and never create;
  * what it prints from the Stripe CLI, which includes the webhook signing secret, is redacted.
"""
import ast
import tomllib
from pathlib import Path

import pytest

from tests.provider_sandbox.conftest import require_test_mode_key
from tests.provider_sandbox.stripe_listener import redact

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
    def test_a_signing_secret_is_not_shown(self):
        line = "Ready! Your webhook signing secret is whsec_0123456789abcdef0123456789abcdef (^C to quit)"

        assert "0123456789abcdef" not in redact(line) and "whsec_***" in redact(line)

    @pytest.mark.parametrize("key", ["sk_test_AbC123xyz789", "rk_test_AbC123xyz789", "sk_live_AbC123xyz789"])
    def test_an_api_key_is_not_shown(self, key):
        shown = redact(f"using {key} for this run")

        assert "AbC123xyz789" not in shown and key[:8] in shown
