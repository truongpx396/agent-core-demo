"""A pin on the OpenTelemetry behaviour the credit reconciliation's heartbeat exists for (scripts/credit_reconcile.py).

A synchronous Gauge is exported ONCE per set: the SDK's last-value aggregation clears the value when it is collected
(`_LastValueAggregation.collect`, verified in opentelemetry-sdk 1.44), and the collector's Prometheus exporter then forgets a series
five minutes after its last update (`metric_expiration`, default 5m, verified against the contrib exporter's README). So a gauge
set by a job that runs every few hours is visible to Prometheus for five minutes in each of them, and an alert on it can never
stay firing; the reconciliation worker therefore re-sets its gauges every minute.

If an SDK release starts re-exporting an unchanged gauge, this test fails, and the heartbeat (and the same disclosure on
agent_usage_export_oldest_pending_age_seconds) should be reconsidered rather than kept out of habit."""
from app.core import metrics
from tests.conftest import _METRIC_READER


def _points(gauge) -> list:
    data = _METRIC_READER.get_metrics_data()
    return [
        point
        for resource_metrics in (data.resource_metrics if data else [])
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == gauge.name
        for point in metric.data.data_points
    ]


def test_a_gauge_is_exported_once_per_set_and_not_again_until_it_is_set_again():
    gauge = metrics.agent_credit_reconcile_max_drift_usd

    gauge.set(7.0)
    first = _points(gauge)
    second = _points(gauge)  # nothing was set in between

    assert [p.value for p in first] == [7.0]
    assert second == []

    gauge.set(0.0)  # the heartbeat: setting again is what makes the next collection carry it
    assert [p.value for p in _points(gauge)] == [0.0]
