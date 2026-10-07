"""The reconciliation command and worker (scripts/credit_reconcile.py): its exit codes, what a failed pass does, the refusal to run without the
gateway's key, and the heartbeat that keeps the drift gauge alive between passes (see tests/core/test_gauge_export.py for why)."""
import asyncio
import os
import signal
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from app.billing import reconcile
from app.billing.reconcile import Finding, Report, Tolerance, Window
from app.core import metrics
from scripts import credit_reconcile
from tests.conftest import metric_value

WINDOW = Window(datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, 18, tzinfo=UTC))
OK = Report(WINDOW, Tolerance())
DRIFT = Report(WINDOW, Tolerance(), findings=[Finding("gateway", "acme", date(2026, 10, 6), D("0"), D("0.5"))])
INCOMPLETE = Report(WINDOW, Tolerance(), incomplete=True)


def count(outcome: str) -> float:
    return metric_value(metrics.agent_credit_reconcile_total, outcome=outcome)


@pytest.mark.parametrize(("report", "code"), [(OK, 0), (DRIFT, 1), (INCOMPLETE, 2), (None, 2)])
def test_the_exit_code_separates_agreement_from_drift_from_a_pass_that_proved_nothing(report, code):
    assert credit_reconcile.exit_code(report) == code


class TestOnePass:
    async def test_a_pass_publishes_its_figures_and_counts_its_outcome(self, monkeypatch):
        async def fake(window, **kwargs):
            return DRIFT

        monkeypatch.setattr(reconcile, "reconcile", fake)
        before = count("drift")

        report = await credit_reconcile.run_pass(None, days=2)

        # The gauge first: ANY read collects every instrument, and collecting clears a synchronous gauge (test_gauge_export.py).
        assert metric_value(metrics.agent_credit_reconcile_max_drift_usd) == 0.5
        assert report is DRIFT and count("drift") - before == 1

    async def test_a_pass_that_raises_is_counted_failed_and_logged_by_class_never_by_message(self, monkeypatch, caplog):
        async def boom(window, **kwargs):
            raise RuntimeError("postgres://user:hunter2@db/appdata refused")

        monkeypatch.setattr(reconcile, "reconcile", boom)
        before = count("failed")

        with caplog.at_level("WARNING"):
            report = await credit_reconcile.run_pass(None, days=2)

        assert report is None and count("failed") - before == 1
        (record,) = [r for r in caplog.records if r.getMessage() == "credit_reconcile_failed"]
        assert record.error_class == "RuntimeError"  # type: ignore[attr-defined]  # `extra` fields are set as record attributes
        assert "hunter2" not in caplog.text and "hunter2" not in str(record.__dict__)

    async def test_a_failed_pass_publishes_nothing_so_it_can_never_read_as_an_all_clear(self, monkeypatch):
        published = []

        async def boom(window, **kwargs):
            raise OSError

        monkeypatch.setattr(reconcile, "reconcile", boom)
        monkeypatch.setattr(reconcile, "publish_gauges", published.append)

        assert await credit_reconcile.run_pass(None, days=2) is None
        assert published == []  # a "0" here would say everything agrees, from a pass that proved nothing


class TestFirstFailureIsVisible:
    def test_priming_adds_zero_to_every_outcome_so_each_series_exists_before_anything_happens(self, monkeypatch):
        added: list[tuple[str, float]] = []

        class Recorder:
            def labels(self, **labels):
                outcome = labels["outcome"]
                return type("Bound", (), {"inc": lambda _self, amount=1: added.append((outcome, amount))})()

        monkeypatch.setattr(metrics, "agent_credit_reconcile_total", Recorder())

        reconcile.prime_outcomes()

        assert sorted(added) == sorted((outcome, 0) for outcome in reconcile.OUTCOMES)

    def test_adding_zero_to_a_counter_really_does_create_its_series(self):
        """The property the priming relies on, read from the SDK: without it priming would be a no-op that looks like a fix."""
        from tests.conftest import _METRIC_READER

        counter = metrics.Counter("agent_test_zero_priming_total", "a throwaway counter for this test", ["outcome"])
        counter.labels(outcome="never_happened").inc(0)

        data = _METRIC_READER.get_metrics_data()
        points = [
            (dict(point.attributes), point.value)
            for resource_metrics in (data.resource_metrics if data else [])
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
            if metric.name == counter.name
            for point in metric.data.data_points
        ]
        assert points == [({"outcome": "never_happened"}, 0)]


class TestTheGatewayKey:
    def test_without_the_key_it_refuses_rather_than_quietly_skipping_the_independent_meter(self, monkeypatch):
        monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

        with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY"):
            credit_reconcile.gateway_client(no_gateway=False)

    def test_no_gateway_is_the_one_way_to_run_without_it(self, monkeypatch):
        monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

        assert credit_reconcile.gateway_client(no_gateway=True) is None

    async def test_the_key_and_address_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-from-env")
        monkeypatch.setenv("LITELLM_URL", "http://gateway.internal:4000")

        client = credit_reconcile.gateway_client(no_gateway=False)

        assert client is not None
        assert client.headers["authorization"] == "Bearer sk-from-env"
        assert str(client.base_url).startswith("http://gateway.internal:4000")
        await client.aclose()


class TestTheWorker:
    async def test_between_passes_it_republishes_the_last_figures_every_heartbeat_and_stops_on_sigterm(self, monkeypatch):
        passes, heartbeats = [], []

        async def fake_pass(client, *, days):
            passes.append(days)
            return DRIFT

        monkeypatch.setattr(credit_reconcile, "run_pass", fake_pass)
        monkeypatch.setattr(reconcile, "publish_gauges", lambda report: heartbeats.append(report))
        monkeypatch.setattr(credit_reconcile, "HEARTBEAT_SECONDS", 0.02)
        monkeypatch.setattr(credit_reconcile, "CREDIT_RECONCILE_INTERVAL_SECONDS", 0.1)
        monkeypatch.setattr(credit_reconcile, "gateway_client", lambda **kw: None)
        asyncio.get_running_loop().call_later(0.35, os.kill, os.getpid(), signal.SIGTERM)

        code = await credit_reconcile.run(loop=True, days=3, as_json=False, no_gateway=True)

        assert code == 0
        assert len(passes) >= 2 and set(passes) == {3}
        assert len(heartbeats) >= 6 and all(report is DRIFT for report in heartbeats)

    async def test_a_failed_pass_leaves_nothing_to_republish_and_the_worker_carries_on(self, monkeypatch):
        passes, heartbeats = [], []

        async def failing_pass(client, *, days):
            passes.append(1)

        monkeypatch.setattr(credit_reconcile, "run_pass", failing_pass)
        monkeypatch.setattr(reconcile, "publish_gauges", lambda report: heartbeats.append(report))
        monkeypatch.setattr(credit_reconcile, "HEARTBEAT_SECONDS", 0.02)
        monkeypatch.setattr(credit_reconcile, "CREDIT_RECONCILE_INTERVAL_SECONDS", 0.06)
        monkeypatch.setattr(credit_reconcile, "gateway_client", lambda **kw: None)
        asyncio.get_running_loop().call_later(0.25, os.kill, os.getpid(), signal.SIGTERM)

        await credit_reconcile.run(loop=True, days=2, as_json=False, no_gateway=True)

        assert len(passes) >= 2 and heartbeats == []

    async def test_one_shot_runs_one_pass_and_returns_its_exit_code(self, monkeypatch, capsys):
        async def fake_pass(client, *, days):
            return DRIFT

        monkeypatch.setattr(credit_reconcile, "run_pass", fake_pass)

        code = await credit_reconcile.run(loop=False, days=2, as_json=True, no_gateway=True)

        assert code == 1
        assert '"outcome": "drift"' in capsys.readouterr().out

    async def test_a_failed_one_shot_says_nothing_was_proven(self, monkeypatch, capsys):
        async def fake_pass(client, *, days):
            return None

        monkeypatch.setattr(credit_reconcile, "run_pass", fake_pass)

        assert await credit_reconcile.run(loop=False, days=2, as_json=False, no_gateway=True) == 2
        assert "Nothing was proven either way" in capsys.readouterr().out
