"""The export worker's entry point (credit_billing/cli/export_worker.py): which providers it serves, that it refuses to idle
when there is nothing to do, and that a misconfiguration stops it instead of exporting nothing quietly. The export logic
itself is credit_billing/export.py (pure rules: tests/test_export_verdicts.py; the database: the integration tier)."""
from contextlib import asynccontextmanager

import pytest

from credit_billing import providers
from credit_billing.cli import export_worker as worker
from credit_billing.cli._runtime import EXIT_DATABASE_UNAVAILABLE
from tests.conftest import SECRET


@pytest.fixture
def configured(monkeypatch):
    def set_(names, secrets=None):
        monkeypatch.setattr(worker, "BILLING_PROVIDERS", names)
        monkeypatch.setattr(worker, "BILLING_WEBHOOK_SECRETS", secrets if secrets is not None else {n: "s" for n in names})

    return set_


def test_it_serves_the_enabled_providers_that_bill_on_usage(configured):
    configured(("fake",))

    assert set(worker.serving()) == {"fake"}


def test_nothing_enabled_means_nothing_to_serve(configured):
    configured(())

    assert worker.serving() == {}


def test_a_provider_that_does_not_bill_on_usage_is_not_served(configured, monkeypatch):
    """A webhook-only provider (a credit-pack seller) has no usage to export."""
    monkeypatch.setattr(providers, "usage_export_providers", lambda names: ())
    configured(("fake",))

    assert worker.serving() == {}


def test_an_enabled_provider_with_no_secret_stops_the_worker_rather_than_exporting_nothing(configured):
    configured(("fake",), secrets={})

    with pytest.raises(providers.UnknownProvider, match="no secret"):
        worker.serving()


async def test_with_nothing_to_serve_it_says_so_and_exits_instead_of_idling_and_looking_healthy(configured, capsys, monkeypatch):
    configured(())

    assert await worker.run(once=True) == 0
    assert "nothing to export" in capsys.readouterr().out.lower()


async def test_once_runs_a_single_pass_and_reports_it(configured, capsys, monkeypatch):
    configured(("fake",))
    passes = []

    async def run_once(adapters):
        passes.append(sorted(adapters))
        return {"sent": 3, "retry": 1}

    @asynccontextmanager
    async def pool():
        yield None

    monkeypatch.setattr(worker.export, "run_once", run_once)
    monkeypatch.setattr(worker, "wired_pool", pool)

    assert await worker.run(once=True) == 0
    assert passes == [["fake"]]
    assert capsys.readouterr().out.strip() == "Export pass: 1 retry, 3 sent."


def test_a_quiet_pass_is_described_as_such():
    assert worker.describe({}) == "Nothing to export." and worker.describe({"sent": 0}) == "Nothing to export."


def test_a_database_that_cannot_be_reached_is_one_line_and_exit_status_3_never_a_traceback_or_a_credential(
    configured, closed_port_url, fast_failure, monkeypatch, capsys
):
    configured(("fake",))
    monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)
    monkeypatch.setattr("sys.argv", ["credit-billing-export-worker", "--once"])

    with pytest.raises(SystemExit) as exited:
        worker.main()

    err = capsys.readouterr().err
    assert exited.value.code == EXIT_DATABASE_UNAVAILABLE
    assert err.startswith("error: cannot open a connection to 127.0.0.1:") and SECRET not in err and "Traceback" not in err
