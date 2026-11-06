"""The ops module's published use cases (`app/domains/ops/usecases.py`): what the published MCP server and the cron digest call, behind the
`MetricsReader` and `IncidentStore` ports, with no tool layer in between."""
from app.domains.ops import metric_checks, usecases
from app.domains.ops.ports import incident_store, metrics_reader


class _Metrics:
    def __init__(self, readings):
        self._readings = readings

    async def fetch_readings(self):
        return self._readings


class _Incidents:
    def __init__(self, rows):
        self._rows = rows
        self.asked_for = []

    async def list_recent_incidents(self, limit=10, status=None):
        self.asked_for.append(status)
        return self._rows


def _quiet():
    return {check.name: 0.0 for check in metric_checks.CHECKS}


async def test_read_metrics_returns_the_readings_and_the_anomalies_among_them():
    readings = _quiet() | {"turn_error_rate": 0.5}  # threshold is 0.05
    with metrics_reader.overriding(_Metrics(readings)):
        got, anomalies = await usecases.read_metrics()

    assert got == readings
    assert len(anomalies) == 1 and "0.5" in anomalies[0]


async def test_the_summary_says_so_when_nothing_is_past_its_threshold():
    with metrics_reader.overriding(_Metrics(_quiet())):
        text = await usecases.fetch_metrics_summary_impl()

    assert "No anomalies" in text and "Anomalies (past" not in text


async def test_the_summary_lists_each_anomaly_after_the_readings():
    with metrics_reader.overriding(_Metrics(_quiet() | {"turn_error_rate": 0.5})):
        text = await usecases.fetch_metrics_summary_impl()

    assert text.startswith(metric_checks.format_readings(_quiet() | {"turn_error_rate": 0.5}))
    assert "Anomalies (past an alert-matching threshold):\n- " in text


async def test_recent_incidents_are_listed_through_the_port_with_the_status_filter():
    rows = [{"id": 7, "status": "open", "summary": "db down", "opened_by": "ana", "created_at": "2026-11-05"}]
    store = _Incidents(rows)
    with incident_store.overriding(store):
        text = await usecases.list_recent_incidents_impl("open")

    assert text == "- #7 [open] db down (opened by ana, 2026-11-05)"
    assert store.asked_for == ["open"]


async def test_no_incidents_is_said_plainly():
    with incident_store.overriding(_Incidents([])):
        assert await usecases.list_recent_incidents_impl(None) == "No incidents on record."


def test_the_policy_permits_the_reads_the_published_server_makes_and_nothing_else():
    ctx = {"tenant": "t", "principal": "p", "claims": {}}

    assert usecases.OPS_POLICY.permit("fetch_metrics", ctx) and usecases.OPS_POLICY.permit("list_recent_incidents", ctx)
    assert not usecases.OPS_POLICY.permit("drop_database", ctx)
    assert not usecases.OPS_POLICY.permit("fetch_metrics", {"tenant": "t", "principal": "", "claims": {}})
