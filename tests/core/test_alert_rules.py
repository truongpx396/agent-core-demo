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
