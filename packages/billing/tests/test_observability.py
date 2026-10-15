"""The alert rules and the dashboard the package ships (spec 011 D22) reference only metrics the package defines.

They are assets another project loads as they are, so a rule that names a metric the package does not emit would be an alert that can
never fire, and a panel on one would be empty: silent failures of exactly the kind these exist to prevent. The repo's own
tests/core/test_billing_package_copies.py checks the other direction (the application's deployed copies have not drifted from these).
"""
import json
import re
from pathlib import Path

import yaml

from credit_billing import metrics

OBS = Path(__file__).resolve().parents[1] / "observability"
DEFINED = {getattr(metrics, name).name for name in dir(metrics) if isinstance(getattr(metrics, name), (metrics.Counter, metrics.Gauge))}
# Counted for an alert and not charted: its paths are alert conditions, not a rate anyone watches.
NOT_CHARTED = {"agent_cost_governance_degraded_total"}


def _names(expression: str) -> set[str]:
    return set(re.findall(r"\b(agent_[a-z_]+?)(?:_bucket|_count|_sum)?\b(?=[\s{\[)(]|$)", expression))


def _rules() -> list[dict]:
    return [rule for group in yaml.safe_load((OBS / "alerts.yml").read_text())["groups"] for rule in group["rules"]]


def _panels() -> list[dict]:
    return [p for p in json.loads((OBS / "credit-billing-core.json").read_text())["panels"] if p["type"] != "row"]


def test_the_package_defines_exactly_these_counters_and_gauges():
    assert DEFINED == {
        "agent_credit_overdraft_total", "agent_billing_webhook_total", "agent_credit_granted_total", "agent_credit_debited_total",
        "agent_credit_enforcement_refused_total", "agent_usage_export_total", "agent_usage_export_oldest_pending_age_seconds",
        "agent_credit_outstanding", "agent_credit_reconcile_max_drift_usd", "agent_credit_reconcile_total",
        "agent_cost_governance_degraded_total",
    }


def test_the_package_ships_the_alerts_for_everything_it_can_fail_to_do_with_what_a_page_needs():
    rules = _rules()

    assert [rule["alert"] for rule in rules] == [
        "BillingWebhookQuarantined", "BillingWebhookFailing", "BillingEventWriteFailing", "CreditDebitFailing", "UsageExportStuck",
        "UsageExportExpired", "UsageExportFailed", "UsageExportEnqueueFailing", "CreditReconcileDrift", "CreditReconcileNotCompleting",
        "CreditGateUnenforced",
    ]
    for rule in rules:
        assert rule["labels"]["severity"] in ("critical", "warning")
        assert rule["annotations"]["summary"] and rule["annotations"]["description"]


def test_every_degraded_path_the_package_counts_has_an_alert_on_it():
    """A degrade-and-continue path nobody alerts on is still silent (the repository's rule for every such path)."""
    watched = set(re.findall(r'agent_cost_governance_degraded_total\{path="([a-z_]+)"\}', "\n".join(rule["expr"] for rule in _rules())))

    assert watched == set(metrics.DEGRADED_PATHS)


def test_every_metric_an_alert_names_is_one_the_package_defines():
    named = set().union(*(_names(rule["expr"]) for rule in _rules()))

    assert named and named <= DEFINED, f"alerts on metrics the package does not emit: {sorted(named - DEFINED)}"


def test_every_metric_a_panel_queries_is_one_the_package_defines():
    queried = set().union(*(_names(t["expr"]) for p in _panels() for t in p["targets"]))

    assert queried == DEFINED - NOT_CHARTED, f"panels do not cover exactly the package's charted metrics: {sorted(queried ^ (DEFINED - NOT_CHARTED))}"


def test_the_dashboard_has_its_own_identity_so_it_never_collides_with_the_applications():
    dashboard = json.loads((OBS / "credit-billing-core.json").read_text())

    assert dashboard["uid"] == "credit-billing-core" and dashboard["uid"] != "credit-billing"


def test_the_dashboard_is_valid_and_its_panels_are_distinct():
    """(Moved here from the application's `tests/core/test_credit_dashboard.py`: the application's dashboard is a copy of this one, and
    `tests/core/test_billing_package_copies.py` fails the build if the copy drifts, so checking the original is checking both.)"""
    dashboard = json.loads((OBS / "credit-billing-core.json").read_text())
    ids = [panel["id"] for panel in dashboard["panels"]]

    assert dashboard["title"] and len(ids) == len(set(ids))


def test_every_query_goes_to_the_provisioned_prometheus_datasource():
    for panel in _panels():
        assert panel["datasource"]["uid"] == "prometheus", panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == "prometheus" and target["expr"], panel["title"]


def test_the_balance_grant_rate_debit_rate_and_export_lag_an_operator_needs_are_there():
    queries = "\n".join(target["expr"] for panel in _panels() for target in panel["targets"])

    for needle in (
        'agent_credit_outstanding{state="available"}',  # balance
        "agent_credit_granted_total",  # grant rate
        "agent_credit_debited_total",  # debit rate
        "agent_usage_export_oldest_pending_age_seconds",  # export lag
        "agent_credit_reconcile_max_drift_usd",  # the independent check
    ):
        assert needle in queries, f"no panel shows {needle}"
