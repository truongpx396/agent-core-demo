"""observability/prometheus/alerts.yml against the metrics the code really records.

An alert on a metric nothing records is silent forever, and nothing about it looks
wrong: the rule loads, the dashboard shows no firing alerts, and the failure it was
written for goes unseen. This file checks, without Prometheus, two cheap facts:
  * every `agent_*` metric an alert expression names is defined in
    app/core/metrics.py (a typo, or a metric someone renamed, fails here);
  * the rules this repo has promised exist (a worker pool that answers nothing).
It does not evaluate the expressions or prove a rule fires; `promtool test rules`
would, and is not wired in here.
"""
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def _rules() -> list[dict]:
    doc = yaml.safe_load((REPO / "observability/prometheus/alerts.yml").read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def _defined_metrics() -> set[str]:
    # The host defines most metrics; the billing core defines its own four (spec 011: it cannot import the host registry).
    source = "\n".join((REPO / name).read_text() for name in ("app/core/metrics.py", "packages/billing/src/credit_billing/metrics.py"))
    defined = set(re.findall(r'^\w+ = (?:Counter|Histogram|Gauge)\(\s*"([a-z_]+)"', source, re.MULTILINE))
    # The authlimit service is Go and defines its metrics as string constants in one file (specs/018): the same rule, a different reader.
    go_metrics = (REPO / "services/authlimit/internal/telemetry/metrics.go").read_text()
    return defined | set(re.findall(r'"(agent_authlimit_[a-z_]+)"', go_metrics))


def test_every_agent_metric_an_alert_names_is_defined_in_the_code():
    defined = _defined_metrics()
    assert defined, "test setup: the metric definitions were not found"
    unknown = []
    for rule in _rules():
        for name in re.findall(r"\b(agent_[a-z_]+?)(?:_bucket|_count|_sum)?\b(?=[\s{\[)(]|$)", rule["expr"]):
            if name not in defined:
                unknown.append(f"{rule['alert']}: {name}")

    assert not unknown, "alerts on metrics nothing records:\n" + "\n".join(unknown)


def _ml_service_metrics() -> set[str]:
    # The ML service is a separate image with its own metrics module (docker/ml-service/ml_service/metrics.py), named `ml_*`.
    source = (REPO / "docker/ml-service/ml_service/metrics.py").read_text()
    return set(re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"(ml_[a-z_]+)"', source))


def test_every_ml_metric_an_alert_names_is_defined_by_the_ml_service():
    defined = _ml_service_metrics()
    assert defined, "test setup: the ML service's metric definitions were not found"
    unknown = []
    for rule in _rules():
        for name in re.findall(r"\b(ml_[a-z_]+?)(?:_bucket|_count|_sum)?\b(?=[\s{\[)(]|$)", rule["expr"]):
            if name not in defined:
                unknown.append(f"{rule['alert']}: {name}")

    assert not unknown, "alerts on ML-service metrics nothing records:\n" + "\n".join(unknown)


def test_a_model_that_is_down_while_the_ml_service_is_up_has_an_alert():
    """ScrapeTargetDown only fires when the whole service is unreachable. A model that failed to load leaves the service up and every
    consumer quietly degrading, so without this rule nobody is told."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "MlServiceModelDown" in rules
    assert "ml_model_state" in rules["MlServiceModelDown"]["expr"]
    assert rules["MlServiceModelDown"]["labels"]["severity"] in {"warning", "critical"}


def test_the_head_specific_fallback_alert_is_gone_with_the_head_and_the_model_down_alert_remains():
    """`ml_classifier_serving` went with the trained-head path (spec 016), so an alert on it would be silent forever. A model that is out is
    still reported by MlServiceModelDown after 5 minutes, which is earlier than the 15 the fallback alert waited."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "RouterClassifierOnFallback" not in rules and "MlServiceModelDown" in rules
    assert not any("ml_classifier_serving" in rule["expr"] for rule in _rules())


def test_a_routed_turn_that_fell_back_to_the_local_model_has_an_alert():
    """The person still got an answer, so nothing else would say that every routed turn is running on the model it was meant to leave."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "ModelRoutedTierFailing" in rules
    assert 'agent_model_route_total{outcome="routed_unreachable_fell_back"}' in rules["ModelRoutedTierFailing"]["expr"]
    assert rules["ModelRoutedTierFailing"]["for"] == "10m"
    assert rules["ModelRoutedTierFailing"]["labels"]["severity"] in {"warning", "critical"}


def test_a_router_that_has_lost_its_label_signal_has_an_alert_that_is_silent_when_the_router_is_off():
    """Routing keeps working from the rules alone, which is exactly why a dead classifier would otherwise go unnoticed. A disabled router (no
    routed turns at all) must not divide by zero into an alert, so the expression requires traffic."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "ModelRouterLabelUnavailable" in rules
    expr = " ".join(str(rules["ModelRouterLabelUnavailable"]["expr"]).split())
    assert "label_none|label_timeout|label_error|label_unknown" in expr and 'outcome!="disabled"' in expr
    assert expr.endswith('and sum(rate(agent_model_route_total{outcome!="disabled"}[15m])) > 0')
    assert rules["ModelRouterLabelUnavailable"]["for"] == "15m" and rules["ModelRouterLabelUnavailable"]["labels"]["severity"] in {"warning", "critical"}


def test_a_gateway_that_cannot_check_any_token_has_an_alert():
    """The gateway fails closed when it cannot confirm the authorization server's signing keys, and a client sees that as an expired
    login (401), so the counter's `jwks_unavailable` outcome is the only place the outage shows (specs/017)."""
    (rule,) = [r for r in _rules() if r["alert"] == "McpGatewayCannotCheckTokens"]

    assert "agent_mcp_gateway_requests_total" in rule["expr"] and 'outcome="jwks_unavailable"' in rule["expr"]


def test_a_worker_pool_that_answers_nothing_has_an_alert():
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "WorkerUnreachable" in rules, "a total worker outage produces no alert"
    assert "agent_worker_unreachable_total" in rules["WorkerUnreachable"]["expr"]
    assert rules["WorkerUnreachable"]["labels"]["severity"] in {"warning", "critical"}


def test_a_turn_whose_consumer_vanished_has_an_alert():
    """An abandoned turn is acked and not retried (process_request always acks), so the person gets
    a generic error at best and nothing at worst while any write the turn made stays committed. The
    outcome is recorded by `_run_graph_stream`'s GeneratorExit branch; without a rule it is a series
    on a dashboard nobody is watching."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "TurnsAbandoned" in rules, "turns that die with their consumer page nobody"
    assert 'outcome="abandoned"' in rules["TurnsAbandoned"]["expr"]
    assert "agent_requests_total" in rules["TurnsAbandoned"]["expr"]
    assert rules["TurnsAbandoned"]["labels"]["severity"] in {"warning", "critical"}


def test_every_degrade_path_that_hides_committed_money_has_an_alert():
    """A metric nobody alerts on is still silent. These are the credit paths where money is committed or
    unverified and no human would otherwise know (constitution V): an event kept whose charge failed, a gate
    that could not read a wallet, and an event that could not be written at all."""
    rules = {rule["alert"]: rule["expr"] for rule in _rules()}

    for alert, path in (
        ("CreditDebitFailing", "credit_debit"),
        ("CreditGateUnenforced", "credit_read"),
        ("UsageEventWriteFailing", "usage_event_write"),
        ("BillingEventWriteFailing", "billing_event_write"),
    ):
        assert alert in rules, f"{alert}: the {path} path hides committed money and has no alert"
        assert f'path="{path}"' in rules[alert], f"{alert} does not watch {path}"


def test_every_way_usage_can_silently_fail_to_reach_a_billing_provider_has_an_alert():
    """Usage that earned money and never reached the provider is revenue nobody was told was lost (constitution V)."""
    rules = {rule["alert"]: rule["expr"] for rule in _rules()}

    for alert, needle in (
        ("UsageExportStuck", "agent_usage_export_oldest_pending_age_seconds"),
        ("UsageExportExpired", 'outcome="expired"'),
        ("UsageExportFailed", 'outcome="failed"'),
        ("UsageExportEnqueueFailing", 'path="export_enqueue"'),
        ("BillingWebhookQuarantined", 'outcome="quarantined"'),
        ("BillingWebhookFailing", 'outcome="failed"'),
    ):
        assert alert in rules, f"{alert}: nothing alerts on {needle}"
        assert needle in rules[alert], f"{alert} does not watch {needle}"


def test_a_reconciliation_that_finds_drift_or_cannot_finish_has_an_alert():
    """The independent check on the meter is only worth its name if someone is told when it disagrees, and when it stops
    being able to say anything (constitution V)."""
    rules = {rule["alert"]: rule["expr"] for rule in _rules()}

    for alert, needle in (
        ("UsageReconcileDrift", "agent_usage_reconcile_max_drift_usd"),
        ("UsageReconcileNotCompleting", 'outcome=~"failed|incomplete"'),
        ("CreditReconcileDrift", "agent_credit_reconcile_max_drift_usd"),
        ("CreditReconcileNotCompleting", 'outcome="failed"'),
    ):
        assert alert in rules, f"{alert}: nothing alerts on {needle}"
        assert needle in rules[alert], f"{alert} does not watch {needle}"


def test_the_stuck_alert_fires_well_before_the_age_limit_expires_events():
    """It is the warning while the events can still be sent, so its threshold must be under the limit's default (30 days)."""
    expr = next(rule["expr"] for rule in _rules() if rule["alert"] == "UsageExportStuck")

    assert int(expr.rsplit(">", 1)[1]) < 30 * 86400


def test_the_authlimit_metrics_come_from_the_go_services_own_file():
    defined = _defined_metrics()

    assert {"agent_authlimit_requests_total", "agent_authlimit_ready", "agent_authlimit_keys_age_seconds"} <= defined, (
        "the Go service's metric names must be visible to the drift check, or an alert on one of them could never be checked"
    )


def test_the_authlimit_degrade_paths_each_have_an_alert():
    """Principle V: a path that leaves a person unaware must have an alert. These are the service's (spec 018, FR-031)."""
    rules = {r["alert"]: r for r in _rules()}
    expected = {
        "AuthlimitCannotCheckTokens": 'reason="keys_unavailable"',   # keys unusable beyond the grace: everything refused
        "AuthlimitKeysStale": "agent_authlimit_keys_age_seconds",   # the first failed refresh, long before the grace ends
        "AuthlimitLimiterStoreDown": 'store="limiter"',             # the limiter failing OPEN: limits silently not enforced
        "AuthlimitRevocationStale": "agent_authlimit_revocation_age_seconds",
        "AuthlimitReplicasDown": "agent_authlimit_ready",
    }

    for name, needle in expected.items():
        assert name in rules, name
        assert needle in rules[name]["expr"], f"{name}: {rules[name]['expr']!r} does not name {needle}"
        assert rules[name]["labels"]["severity"] in {"warning", "critical"}, "only severities the alert routing knows"


def test_the_replica_alert_does_not_fire_forever_where_the_service_is_not_deployed():
    """Production does not run authlimit yet. An `absent()` alone would fire there for ever; the expression only counts a vanished service
    that was reporting half an hour ago."""
    (rule,) = [r for r in _rules() if r["alert"] == "AuthlimitReplicasDown"]

    assert "absent(agent_authlimit_ready)" in rule["expr"] and "offset 30m" in rule["expr"]
