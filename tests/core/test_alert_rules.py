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
    return set(re.findall(r'^\w+ = (?:Counter|Histogram|Gauge)\(\s*"([a-z_]+)"', source, re.MULTILINE))


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


def test_a_classifier_that_has_been_on_its_fallback_for_a_while_has_an_alert():
    """The fallback keeps routing working, which is exactly why a long-dead primary would otherwise go unnoticed."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "RouterClassifierOnFallback" in rules
    assert 'ml_classifier_serving{served_by="fallback"}' in rules["RouterClassifierOnFallback"]["expr"]
    assert rules["RouterClassifierOnFallback"]["for"] == "15m"
    assert rules["RouterClassifierOnFallback"]["labels"]["severity"] in {"warning", "critical"}


def test_an_escalated_turn_that_fell_back_to_the_local_model_has_an_alert():
    """The person still got an answer, so nothing else would say that every escalated turn is running on the model it was meant to leave."""
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "ModelRouteStrongFailing" in rules
    assert 'agent_model_route_total{outcome="strong_failed_fell_back"}' in rules["ModelRouteStrongFailing"]["expr"]
    assert rules["ModelRouteStrongFailing"]["for"] == "10m"
    assert rules["ModelRouteStrongFailing"]["labels"]["severity"] in {"warning", "critical"}


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
