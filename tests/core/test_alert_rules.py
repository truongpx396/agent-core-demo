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
    source = (REPO / "app/core/metrics.py").read_text()
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


def test_a_worker_pool_that_answers_nothing_has_an_alert():
    rules = {rule["alert"]: rule for rule in _rules()}

    assert "WorkerUnreachable" in rules, "a total worker outage produces no alert"
    assert "agent_worker_unreachable_total" in rules["WorkerUnreachable"]["expr"]
    assert rules["WorkerUnreachable"]["labels"]["severity"] in {"warning", "critical"}


def test_every_degrade_path_that_hides_committed_money_has_an_alert():
    """A metric nobody alerts on is still silent. These are the credit paths where money is committed or
    unverified and no human would otherwise know (constitution V): an event kept whose charge failed, a gate
    that could not read a wallet, and an event that could not be written at all."""
    rules = {rule["alert"]: rule["expr"] for rule in _rules()}

    for alert, path in (
        ("CreditDebitFailing", "credit_debit"),
        ("CreditGateUnenforced", "credit_read"),
        ("UsageEventWriteFailing", "usage_event_write"),
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


def test_the_stuck_alert_fires_well_before_the_age_limit_expires_events():
    """It is the warning while the events can still be sent, so its threshold must be under the limit's default (30 days)."""
    expr = next(rule["expr"] for rule in _rules() if rule["alert"] == "UsageExportStuck")

    assert int(expr.rsplit(">", 1)[1]) < 30 * 86400
