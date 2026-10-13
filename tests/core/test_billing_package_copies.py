"""The agent app keeps COPIES of three things the billing package owns, because docker-compose, Prometheus and Grafana read fixed
paths in this repo (specs/011-billing-module-boundary D22). The package's files are canonical; this fails the build if a copy drifts.

  * `postgres-init/20-credit-wallet.sql` and `22-billing.sql` must be byte-equal to the scripts the package ships, because a fresh
    volume is built from the former and every other project from the latter: the schema is identical by construction (SC-005);
  * `observability/prometheus/alerts.yml` must carry the package's two alert rules with the same name, expression, `for`, labels and
    summary (descriptions differ on purpose: each says where its own deployment keeps the schema scripts);
  * the app's combined 'Credit Billing' dashboard must carry each of the package dashboard's panels with the same type and the same
    queries (the package's overdraft panel has one of the app's two, since the other metric is the app's).

Each comparison has a test that it can fail, so none of them passes by comparing a thing with itself.
"""
import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "packages" / "billing"
SQL = PACKAGE / "src" / "credit_billing" / "sql"
APP_ALERTS = REPO / "observability" / "prometheus" / "alerts.yml"
APP_DASHBOARD = REPO / "observability" / "grafana" / "dashboards" / "credit-billing.json"

# A package panel whose title was shortened because the app's version also shows an app-only metric.
TITLE_IN_THE_APP = {"Overdraft debits": "Overdraft debits and refused turns"}


# --- the schema scripts --------------------------------------------------------------------------------------------

def test_the_package_ships_the_scripts_this_test_compares():
    assert sorted(path.name for path in SQL.glob("*.sql")) == ["20-credit-wallet.sql", "22-billing.sql"]


@pytest.mark.parametrize("name", ["20-credit-wallet.sql", "22-billing.sql"])
def test_the_init_script_is_byte_equal_to_the_one_the_package_ships(name):
    assert (REPO / "postgres-init" / name).read_bytes() == (SQL / name).read_bytes(), (
        f"postgres-init/{name} drifted from packages/billing/src/credit_billing/sql/{name}: edit the package's and copy it over"
    )


def test_the_byte_comparison_would_notice_a_single_changed_byte(tmp_path):
    original = (SQL / "20-credit-wallet.sql").read_bytes()
    drifted = tmp_path / "20-credit-wallet.sql"
    drifted.write_bytes(original + b" ")

    assert drifted.read_bytes() != original


# --- the alert rules ------------------------------------------------------------------------------------------------

COMPARED = ("alert", "expr", "for", "labels")


def _rules(path: Path) -> dict[str, dict]:
    return {rule["alert"]: rule for group in yaml.safe_load(path.read_text())["groups"] for rule in group["rules"]}


def _same(package_rule: dict, app_rule: dict) -> list[str]:
    drift = [key for key in COMPARED if package_rule.get(key) != app_rule.get(key)]
    if package_rule["annotations"]["summary"] != app_rule["annotations"]["summary"]:
        drift.append("annotations.summary")
    return drift


def test_the_application_carries_each_of_the_packages_alert_rules_unchanged():
    package_rules, app_rules = _rules(PACKAGE / "observability" / "alerts.yml"), _rules(APP_ALERTS)

    assert set(package_rules) == {"BillingWebhookQuarantined", "BillingWebhookFailing"}
    for name, rule in package_rules.items():
        assert name in app_rules, f"{name} is in the package but not in observability/prometheus/alerts.yml"
        assert _same(rule, app_rules[name]) == [], f"{name} drifted between the package and the application"


@pytest.mark.parametrize("field", ["expr", "for", "labels", "summary"])
def test_the_rule_comparison_would_notice_drift_in_each_field(field):
    rule = {"alert": "A", "expr": "up", "for": "5m", "labels": {"severity": "critical"}, "annotations": {"summary": "s", "description": "d"}}
    drifted = json.loads(json.dumps(rule))
    if field == "summary":
        drifted["annotations"]["summary"] = "changed"
    elif field == "labels":
        drifted["labels"]["severity"] = "warning"
    else:
        drifted[field] = "changed"

    assert _same(rule, drifted)
    assert _same(rule, json.loads(json.dumps(rule))) == []


# --- the dashboard --------------------------------------------------------------------------------------------------

def _panels(path: Path) -> dict[str, dict]:
    return {p["title"]: p for p in json.loads(path.read_text())["panels"] if p["type"] != "row"}


def _queries(panel: dict) -> set[tuple[str, str]]:
    return {(t["expr"], t.get("legendFormat", "")) for t in panel["targets"]}


def test_the_applications_dashboard_carries_each_of_the_packages_panels():
    package_panels, app_panels = _panels(PACKAGE / "observability" / "credit-billing-core.json"), _panels(APP_DASHBOARD)

    assert len(package_panels) == 4
    for title, panel in package_panels.items():
        app_panel = app_panels.get(TITLE_IN_THE_APP.get(title, title))
        assert app_panel is not None, f"the application's dashboard has no panel for {title!r}"
        assert panel["type"] == app_panel["type"]
        assert _queries(panel) <= _queries(app_panel), f"{title!r}: a query of the package's panel is not in the application's"


def test_the_panel_comparison_would_notice_a_changed_query():
    panel = {"targets": [{"expr": "sum(rate(a_total[5m]))", "legendFormat": "x"}]}
    changed = {"targets": [{"expr": "sum(rate(a_total[1h]))", "legendFormat": "x"}]}

    assert not _queries(panel) <= _queries(changed)
