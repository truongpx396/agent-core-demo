"""`credit-billing-usage-sweep` (credit_billing/cli/usage_sweep.py): the words the operator reads and the window it uses (hermetic, the delete is
monkeypatched)."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from credit_billing.cli import usage_sweep as usage_events_sweep
from credit_billing.cli._runtime import EXIT_DATABASE_UNAVAILABLE
from credit_billing.retention import SWEEP_BATCH_SIZE, SWEEP_MAX_BATCHES, SweepResult
from credit_billing.settings import (
    BILLING_USAGE_EVENT_RETENTION_DAYS as USAGE_EVENT_RETENTION_DAYS,
)
from tests.conftest import SECRET


@pytest.fixture(autouse=True)
def no_pool(monkeypatch):
    @asynccontextmanager
    async def pool():
        yield None

    monkeypatch.setattr(usage_events_sweep, "wired_pool", pool)


def _result(**kw) -> SweepResult:
    return SweepResult(**{"deleted": 0, "outbox_cleared": 0, "held_back": 0, "complete": True, **kw})


def test_run_sweep_uses_the_configured_retention_and_returns_what_was_done(monkeypatch):
    seen = {}

    async def fake_sweep(*, older_than_days):
        seen["days"] = older_than_days
        return _result(deleted=12)

    monkeypatch.setattr(usage_events_sweep, "sweep_old_events", fake_sweep)

    assert asyncio.run(usage_events_sweep.run_sweep()).deleted == 12
    assert seen["days"] == USAGE_EVENT_RETENTION_DAYS


def test_run_sweep_honors_a_custom_window(monkeypatch):
    seen = {}

    async def fake_sweep(*, older_than_days):
        seen["days"] = older_than_days
        return _result()

    monkeypatch.setattr(usage_events_sweep, "sweep_old_events", fake_sweep)

    asyncio.run(usage_events_sweep.run_sweep(older_than_days=90))

    assert seen["days"] == 90


def test_an_empty_run_says_so():
    assert usage_events_sweep.describe(_result()) == "Nothing to sweep."


def test_a_run_says_what_it_deleted_and_what_went_with_it():
    text = usage_events_sweep.describe(_result(deleted=12, outbox_cleared=3))

    assert "Deleted 12 usage event(s)." in text and "Cleared 3 finished export row(s)" in text


def test_a_run_that_hit_its_ceiling_tells_the_operator_to_run_it_again():
    ceiling = SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE

    assert "run it again" in usage_events_sweep.describe(_result(deleted=ceiling, complete=False))
    assert "run it again" not in usage_events_sweep.describe(_result(deleted=ceiling))


def test_events_kept_because_their_export_never_finished_are_called_out_not_buried():
    text = usage_events_sweep.describe(_result(deleted=5, held_back=2))

    assert "KEPT 2 old event(s)" in text and "pending, failed or expired" in text


def test_a_database_that_cannot_be_reached_is_one_line_and_exit_status_3_never_a_traceback_or_a_credential(
    closed_port_url, fast_failure, monkeypatch, capsys
):
    monkeypatch.undo()  # the autouse pool stub would hide the very thing under test
    monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    from credit_billing.cli import _runtime

    monkeypatch.setattr(_runtime, "CONNECT_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(SystemExit) as exited:
        usage_events_sweep.main()

    err = capsys.readouterr().err
    assert exited.value.code == EXIT_DATABASE_UNAVAILABLE
    assert err.startswith("error: cannot open a connection to 127.0.0.1:") and SECRET not in err and "Traceback" not in err
