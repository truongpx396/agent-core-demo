"""Tests for app/domains/ops/metrics_client.py. `detect_anomalies`/
`format_readings` are pure functions, tested directly against synthetic
readings — no network. `_query_one`/`fetch_readings` mock `httpx.get`
(same convention as tests/agent/test_model_resolver.py::_FakeResponse) so
these stay hermetic (no live Prometheus).
"""
from app.domains.ops import metrics_client


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


class TestDetectAnomalies:
    def test_no_anomalies_when_every_reading_is_within_threshold(self):
        readings = {check.name: 0.0 for check in metrics_client.CHECKS}
        assert metrics_client.detect_anomalies(readings) == []

    def test_flags_a_reading_past_its_threshold(self):
        readings = {check.name: 0.0 for check in metrics_client.CHECKS}
        readings["turn_error_rate"] = 0.5  # threshold is 0.05
        anomalies = metrics_client.detect_anomalies(readings)
        assert len(anomalies) == 1
        assert "0.5" in anomalies[0]

    def test_a_none_reading_is_never_flagged(self):
        readings = {check.name: None for check in metrics_client.CHECKS}
        assert metrics_client.detect_anomalies(readings) == []

    def test_a_reading_exactly_at_threshold_is_not_an_anomaly(self):
        readings = {check.name: 0.0 for check in metrics_client.CHECKS}
        readings["p95_latency_seconds"] = 30  # threshold is > 30, not >=
        assert metrics_client.detect_anomalies(readings) == []


class TestFormatReadings:
    def test_formats_a_known_value(self):
        readings = {"turn_error_rate": 0.01}
        text = metrics_client.format_readings(readings)
        assert "0.01" in text

    def test_formats_an_unknown_value(self):
        readings = {"turn_error_rate": None}
        text = metrics_client.format_readings(readings)
        assert "unknown" in text


class TestQueryOne:
    def test_returns_the_value_on_a_successful_query(self, monkeypatch):
        monkeypatch.setattr(
            metrics_client.httpx,
            "get",
            lambda *a, **kw: _FakeResponse(
                {"data": {"result": [{"metric": {}, "value": [0, "0.5"]}]}}
            ),
        )
        assert metrics_client._query_one("some_expr") == 0.5

    def test_returns_none_on_an_empty_result_vector(self, monkeypatch):
        monkeypatch.setattr(
            metrics_client.httpx,
            "get",
            lambda *a, **kw: _FakeResponse({"data": {"result": []}}),
        )
        assert metrics_client._query_one("some_expr") is None

    def test_returns_none_on_a_connection_failure(self, monkeypatch):
        def _raise(*a, **kw):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(metrics_client.httpx, "get", _raise)
        assert metrics_client._query_one("some_expr") is None

    def test_fetch_readings_covers_every_check(self, monkeypatch):
        monkeypatch.setattr(
            metrics_client.httpx,
            "get",
            lambda *a, **kw: _FakeResponse({"data": {"result": []}}),
        )
        readings = metrics_client.fetch_readings()
        assert set(readings) == {check.name for check in metrics_client.CHECKS}
