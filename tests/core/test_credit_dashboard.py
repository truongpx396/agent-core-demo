"""observability/grafana/dashboards/credit-billing.json against the metrics the code really records (specs/010 T028).

A panel on a metric nothing records stays empty forever and looks like "no activity", which is the wrong thing to see on a billing
dashboard. This checks, without Grafana, what is cheap to check: the file parses, panel ids are unique, every query is against the
provisioned Prometheus datasource, every `agent_*` metric a query names is defined in app/core/metrics.py, and the figures the task
asked for (balance, grant and debit rates, export lag) are really there."""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DASHBOARD = REPO / "observability/grafana/dashboards/credit-billing.json"


def _panels() -> list[dict]:
    return [panel for panel in json.loads(DASHBOARD.read_text())["panels"] if panel["type"] != "row"]


def _defined_metrics() -> set[str]:
    source = (REPO / "app/core/metrics.py").read_text()
    return set(re.findall(r'^\w+ = (?:Counter|Histogram|Gauge)\(\s*"([a-z_]+)"', source, re.MULTILINE))


def _names(expr: str) -> set[str]:
    return set(re.findall(r"\b(agent_[a-z_]+?)(?:_bucket|_count|_sum)?\b(?=[\s{\[)(]|$)", expr))


def test_the_dashboard_is_valid_and_its_panels_are_distinct():
    dashboard = json.loads(DASHBOARD.read_text())
    ids = [panel["id"] for panel in dashboard["panels"]]

    assert dashboard["uid"] == "credit-billing" and dashboard["title"]
    assert len(ids) == len(set(ids))


def test_every_query_goes_to_the_provisioned_prometheus_datasource():
    for panel in _panels():
        assert panel["datasource"]["uid"] == "prometheus", panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == "prometheus" and target["expr"], panel["title"]


def test_every_metric_a_panel_queries_is_recorded_by_the_code():
    defined, unknown = _defined_metrics(), []
    for panel in _panels():
        for target in panel["targets"]:
            unknown += [f"{panel['title']}: {name}" for name in _names(target["expr"]) if name not in defined]

    assert not unknown, "panels on metrics nothing records:\n" + "\n".join(unknown)


def test_the_balance_grant_rate_debit_rate_and_export_lag_the_task_asked_for_are_there():
    queries = "\n".join(target["expr"] for panel in _panels() for target in panel["targets"])

    for needle in (
        'agent_credit_outstanding{state="available"}',  # balance
        "agent_credit_granted_total",  # grant rate
        "agent_credit_debited_total",  # debit rate
        "agent_usage_export_oldest_pending_age_seconds",  # export lag
        "agent_credit_reconcile_max_drift_usd",  # the independent check
    ):
        assert needle in queries, f"no panel shows {needle}"
