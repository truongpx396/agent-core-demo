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
DEFINED = {getattr(metrics, name).name for name in dir(metrics) if isinstance(getattr(metrics, name), metrics.Counter)}


def _names(expression: str) -> set[str]:
    return set(re.findall(r"\b(agent_[a-z_]+?)(?:_bucket|_count|_sum)?\b(?=[\s{\[)(]|$)", expression))


def _rules() -> list[dict]:
    return [rule for group in yaml.safe_load((OBS / "alerts.yml").read_text())["groups"] for rule in group["rules"]]


def _panels() -> list[dict]:
    return [p for p in json.loads((OBS / "credit-billing-core.json").read_text())["panels"] if p["type"] != "row"]


def test_the_package_defines_exactly_four_counters():
    assert DEFINED == {
        "agent_credit_overdraft_total", "agent_billing_webhook_total", "agent_credit_granted_total", "agent_credit_debited_total",
    }


def test_the_package_ships_the_two_webhook_alerts_with_what_a_page_needs():
    rules = _rules()

    assert [rule["alert"] for rule in rules] == ["BillingWebhookQuarantined", "BillingWebhookFailing"]
    for rule in rules:
        assert rule["labels"]["severity"] == "critical"
        assert rule["annotations"]["summary"] and rule["annotations"]["description"]
        assert "increase(agent_billing_webhook_total" in rule["expr"]


def test_every_metric_an_alert_names_is_one_the_package_defines():
    named = set().union(*(_names(rule["expr"]) for rule in _rules()))

    assert named and named <= DEFINED, f"alerts on metrics the package does not emit: {sorted(named - DEFINED)}"


def test_every_metric_a_panel_queries_is_one_the_package_defines():
    queried = set().union(*(_names(t["expr"]) for p in _panels() for t in p["targets"]))

    assert queried == DEFINED - set(), f"panels do not cover exactly the package's counters: {sorted(queried ^ DEFINED)}"


def test_the_dashboard_has_its_own_identity_so_it_never_collides_with_the_applications():
    dashboard = json.loads((OBS / "credit-billing-core.json").read_text())

    assert dashboard["uid"] == "credit-billing-core" and dashboard["uid"] != "credit-billing"
