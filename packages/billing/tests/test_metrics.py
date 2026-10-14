"""The package's own counters (specs/011-billing-module-boundary D23): their scope, and the series created at 0 for the alerts.

Why this matters: the collector's Prometheus exporter turns the OpenTelemetry scope into an `otel_scope_name` label, and a bare
`increase(...[10m]) > 0` rule cannot count the FIRST sample of a series that did not exist before. The alerts on
`agent_billing_webhook_total` (quarantined, failed) are exactly such rules, so the series they look at must exist at 0 from the moment
the process is configured.
"""
import ast
import typing
import uuid
from pathlib import Path

import pytest

from credit_billing import http, metrics, webhooks
from tests.conftest import has_series, metric_value, scopes_of

HTTP_SOURCE = Path(http.__file__).read_text(encoding="utf-8")


def test_the_counters_are_recorded_under_the_packages_own_scope():
    metrics.agent_credit_granted_total.labels(source="promo").inc(1)

    assert scopes_of(metrics.agent_credit_granted_total) == {"credit_billing"}


def test_the_scope_carries_no_version_so_a_release_never_forks_the_series():
    source = Path(metrics.__file__).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "get_meter"]

    assert len(calls) == 1 and len(calls[0].args) == 1 and not calls[0].keywords, "get_meter takes the scope name and nothing else"


def test_initialising_creates_every_series_the_alerts_look_at_at_zero():
    provider = f"prov-{uuid.uuid4().hex[:8]}"

    metrics.initialise_webhook_series([provider])

    for outcome in metrics.WEBHOOK_OUTCOMES:
        if outcome != "unknown_provider":
            assert has_series(metrics.agent_billing_webhook_total, provider=provider, outcome=outcome), outcome
            assert metric_value(metrics.agent_billing_webhook_total, provider=provider, outcome=outcome) == 0
    assert has_series(metrics.agent_billing_webhook_total, provider="unknown", outcome="unknown_provider")
    assert not has_series(metrics.agent_billing_webhook_total, provider=provider, outcome="unknown_provider"), "that outcome never carries a provider"


def test_initialising_twice_is_harmless_and_never_adds_to_a_count():
    provider = f"prov-{uuid.uuid4().hex[:8]}"
    metrics.initialise_webhook_series([provider])
    metrics.agent_billing_webhook_total.labels(provider=provider, outcome="quarantined").inc()

    metrics.initialise_webhook_series([provider])

    assert metric_value(metrics.agent_billing_webhook_total, provider=provider, outcome="quarantined") == 1


def test_validating_the_configuration_creates_the_series_for_the_providers_it_serves(monkeypatch):
    provider = f"prov-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(http, "configured_providers", lambda: {provider: object()})

    http.validate_configuration()

    assert has_series(metrics.agent_billing_webhook_total, provider=provider, outcome="quarantined")
    assert has_series(metrics.agent_billing_webhook_total, provider=provider, outcome="failed")


def test_every_outcome_the_code_can_emit_is_one_the_series_are_created_for():
    """If the route or the inbox learned a new outcome and this list did not, the new outcome's first event would be invisible."""
    from_the_inbox = set(typing.get_args(webhooks.Outcome))
    counted_by_the_route = {
        call.args[1].value
        for call in ast.walk(ast.parse(HTTP_SOURCE))
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_count"
        and len(call.args) == 2 and isinstance(call.args[1], ast.Constant)
    }

    assert from_the_inbox | counted_by_the_route == set(metrics.WEBHOOK_OUTCOMES)


@pytest.mark.parametrize("outcome", ["quarantined", "failed"])
def test_the_two_alerted_outcomes_are_among_them(outcome):
    assert outcome in metrics.WEBHOOK_OUTCOMES


# --- the paths and outcomes spec 013 moved into the package ------------------------------------------------------------

def _paths_counted_in(module) -> set[str]:
    """Every literal `path` a module passes to `agent_cost_governance_degraded_total.labels(path=...)` or to its own `_degrade(...)` helper."""
    found: set[str] = set()
    for call in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
        if not isinstance(call, ast.Call):
            continue
        if getattr(call.func, "id", "") == "_degrade" and call.args and isinstance(call.args[0], ast.Constant):
            found.add(call.args[0].value)
        if getattr(call.func, "attr", "") == "labels":
            found |= {kw.value.value for kw in call.keywords if kw.arg == "path" and isinstance(kw.value, ast.Constant)}
    return found


def test_every_degraded_path_the_code_can_count_is_one_the_alerts_were_written_for():
    """A new path nobody alerts on would be a silent failure: add it to DEGRADED_PATHS and give it an alert in observability/alerts.yml."""
    from credit_billing import admission, usage

    assert _paths_counted_in(usage) | _paths_counted_in(admission) == set(metrics.DEGRADED_PATHS)


def test_every_reconcile_outcome_the_code_records_is_one_the_priming_creates():
    from credit_billing import reconcile
    from credit_billing.cli import reconcile as cli

    recorded = {
        call.args[0].value
        for call in ast.walk(ast.parse(Path(cli.__file__).read_text(encoding="utf-8")))
        if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "record_outcome" and isinstance(call.args[0], ast.Constant)
    }

    assert recorded <= set(reconcile.OUTCOMES) and "failed" in recorded
