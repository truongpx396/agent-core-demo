"""The wallet reconciliation command and worker (credit_billing/cli/reconcile.py): its exit codes, what a failed pass does, the heartbeat that keeps
the gauge alive between passes (a synchronous gauge is exported once per set and the collector forgets a series five minutes later), and a database
that cannot be reached."""
import asyncio
import os
import signal
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from credit_billing import metrics, reconcile
from credit_billing.cli import reconcile as cli
from credit_billing.cli._runtime import EXIT_DATABASE_UNAVAILABLE
from credit_billing.reconcile import Finding, Report, Window
from tests.conftest import SECRET, metric_value

WINDOW = Window(datetime(2026, 10, 6, tzinfo=UTC), datetime(2026, 10, 6, 18, tzinfo=UTC))
OK = Report(WINDOW)
DRIFT = Report(WINDOW, findings=[Finding("acme", date(2026, 10, 6), 1, D("500"), D("0.5"))])


def count(outcome: str) -> float:
    return metric_value(metrics.agent_credit_reconcile_total, outcome=outcome)


@pytest.fixture(autouse=True)
def no_pool(monkeypatch):
    @asynccontextmanager
    async def pool():
        yield None

    monkeypatch.setattr(cli, "wired_pool", pool)


@pytest.mark.parametrize(("report", "code"), [(OK, 0), (DRIFT, 1), (None, 2)])
def test_the_exit_code_separates_agreement_from_drift_from_a_pass_that_proved_nothing(report, code):
    assert cli.exit_code(report) == code


class TestOnePass:
    async def test_a_pass_publishes_its_figures_and_counts_its_outcome(self, monkeypatch):
        async def fake(window, **kwargs):
            return DRIFT

        monkeypatch.setattr(reconcile, "reconcile", fake)
        before = count("drift")

        report = await cli.run_pass(days=2)

        # The gauge first: ANY read collects every instrument, and collecting clears a synchronous gauge.
        assert metric_value(metrics.agent_credit_reconcile_max_drift_usd) == 0.5
        assert report is DRIFT and count("drift") - before == 1

    async def test_a_pass_that_raises_is_counted_failed_and_logged_by_class_never_by_message(self, monkeypatch, caplog):
        async def boom(window, **kwargs):
            raise RuntimeError("postgres://user:hunter2@db/appdata refused")

        monkeypatch.setattr(reconcile, "reconcile", boom)
        before = count("failed")

        with caplog.at_level("WARNING"):
            report = await cli.run_pass(days=2)

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

        assert await cli.run_pass(days=2) is None
        assert published == []  # a "0" here would say everything is charged, from a pass that proved nothing


class TestTheWorker:
    async def test_between_passes_it_republishes_the_last_figures_every_heartbeat_and_stops_on_sigterm(self, monkeypatch):
        passes, heartbeats = [], []

        async def fake_pass(*, days):
            passes.append(days)
            return DRIFT

        monkeypatch.setattr(cli, "run_pass", fake_pass)
        monkeypatch.setattr(reconcile, "publish_gauges", lambda report: heartbeats.append(report))
        monkeypatch.setattr(cli, "HEARTBEAT_SECONDS", 0.02)
        monkeypatch.setattr(cli, "CREDIT_RECONCILE_INTERVAL_SECONDS", 0.1)
        asyncio.get_running_loop().call_later(0.35, os.kill, os.getpid(), signal.SIGTERM)

        code = await cli.run(loop=True, days=3, as_json=False)

        assert code == 0
        assert len(passes) >= 2 and set(passes) == {3}
        assert len(heartbeats) >= 6 and all(report is DRIFT for report in heartbeats)

    async def test_a_failed_pass_leaves_nothing_to_republish_and_the_worker_carries_on(self, monkeypatch):
        passes, heartbeats = [], []

        async def failing_pass(*, days):
            passes.append(1)

        monkeypatch.setattr(cli, "run_pass", failing_pass)
        monkeypatch.setattr(reconcile, "publish_gauges", lambda report: heartbeats.append(report))
        monkeypatch.setattr(cli, "HEARTBEAT_SECONDS", 0.02)
        monkeypatch.setattr(cli, "CREDIT_RECONCILE_INTERVAL_SECONDS", 0.06)
        asyncio.get_running_loop().call_later(0.25, os.kill, os.getpid(), signal.SIGTERM)

        await cli.run(loop=True, days=2, as_json=False)

        assert len(passes) >= 2 and heartbeats == []

    async def test_one_shot_runs_one_pass_and_returns_its_exit_code(self, monkeypatch, capsys):
        async def fake_pass(*, days):
            return DRIFT

        monkeypatch.setattr(cli, "run_pass", fake_pass)

        code = await cli.run(loop=False, days=2, as_json=True)

        assert code == 1
        assert '"outcome": "drift"' in capsys.readouterr().out

    async def test_a_failed_one_shot_says_nothing_was_proven(self, monkeypatch, capsys):
        async def fake_pass(*, days):
            return None

        monkeypatch.setattr(cli, "run_pass", fake_pass)

        assert await cli.run(loop=False, days=2, as_json=False) == 2
        assert "Nothing was proven either way" in capsys.readouterr().out

    async def test_it_primes_every_outcome_series_before_the_first_pass(self, monkeypatch):
        primed = []

        async def fake_pass(*, days):
            return OK

        monkeypatch.setattr(cli, "run_pass", fake_pass)
        monkeypatch.setattr(reconcile, "prime_outcomes", lambda: primed.append(True))

        await cli.run(loop=False, days=2, as_json=False)

        assert primed == [True]


class TestArgumentsAndTheDatabase:
    def test_days_outside_the_window_the_setting_allows_is_invalid_input(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["credit-billing-reconcile", "--days", "36"])

        with pytest.raises(SystemExit) as exited:
            cli.main()

        assert exited.value.code == 2 and "--days must be between 1 and 35" in capsys.readouterr().err

    def test_a_database_that_cannot_be_reached_is_one_line_and_exit_status_3_never_a_traceback_or_a_credential(
        self, closed_port_url, fast_failure, monkeypatch, capsys
    ):
        monkeypatch.undo()  # the autouse pool stub would hide the very thing under test
        monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        from credit_billing.cli import _runtime

        monkeypatch.setattr(_runtime, "CONNECT_TIMEOUT_SECONDS", 0.5)
        monkeypatch.setattr("sys.argv", ["credit-billing-reconcile"])

        with pytest.raises(SystemExit) as exited:
            cli.main()

        err = capsys.readouterr().err
        assert exited.value.code == EXIT_DATABASE_UNAVAILABLE
        assert err.startswith("error: cannot open a connection to 127.0.0.1:") and SECRET not in err and "Traceback" not in err
