"""The `credit-billing-migrate` command: arguments, output, exit statuses (specs/012-billing-migrate-idempotency/contracts/credit-billing-migrate.md).

The migration logic is `tests/test_migrate.py` and the real database is `tests/integration/test_migrate_real_postgres.py`; this file pins what an
operator or a pipeline sees. Statuses: 0 done or current, 1 `--check` found work, 2 invalid arguments, 3 database unreachable, 4 a migration did
not complete. No traceback, and never the connection's user or password.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from credit_billing import migrate
from credit_billing.cli import migrate as cli
from credit_billing.cli._runtime import EXIT_DATABASE_UNAVAILABLE
from tests.conftest import SECRET

WHEN = datetime(2026, 10, 1, 12, 30, 5, tzinfo=UTC)
CHECKSUM = "ab12cd34" + "0" * 56


def script(name, state="applied", recorded=CHECKSUM, applied_at=WHEN):
    return migrate.ScriptStatus(name, state, CHECKSUM, recorded if state != "pending" else None, applied_at if state != "pending" else None)


def status(*scripts, unknown=(), exists=True):
    return migrate.Status(list(scripts), list(unknown), exists)


@pytest.fixture
def no_database(monkeypatch):
    """The command with the pool and the migration module replaced, so only the command's own behaviour runs."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    @asynccontextmanager
    async def pool():
        yield None

    monkeypatch.setattr(cli, "wired_pool", pool)


def run(argv, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)
    captured = capsys.readouterr()
    return exited.value.code, captured.out, captured.err


# --- arguments -----------------------------------------------------------------------------------------------------------

def test_the_defaults_apply_with_a_ten_second_lock_timeout():
    args = cli.build_parser().parse_args([])

    assert (args.status, args.check, args.lock_timeout_seconds) == (False, False, 10.0)


def test_status_and_check_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["--status", "--check"])

    assert exited.value.code == 2 and "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "nan"])
def test_a_lock_timeout_that_is_not_above_zero_is_invalid_input(bad, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.build_parser().parse_args(["--lock-timeout-seconds", bad])

    assert exited.value.code == 2 and "--lock-timeout-seconds" in capsys.readouterr().err


def test_a_fractional_lock_timeout_is_accepted():
    assert cli.build_parser().parse_args(["--lock-timeout-seconds", "0.5"]).lock_timeout_seconds == 0.5


# --- output --------------------------------------------------------------------------------------------------------------

def test_status_lists_every_state_one_line_per_script_and_the_unknown_ones_last():
    text = cli.format_status(
        status(script("20-a.sql"), script("22-b.sql", "changed", recorded="f" * 64), script("23-c.sql", "pending"), unknown=["99-z.sql"])
    )

    assert text.splitlines() == [
        "applied  20-a.sql   2026-10-01T12:30:05Z",
        "changed  22-b.sql   recorded ffffffff…  shipped ab12cd34…",
        "pending  23-c.sql",
        "unknown  99-z.sql   recorded by another version; left alone",
    ]


def test_the_no_record_footer_appears_only_when_the_record_table_is_absent():
    absent = cli.format_status(status(script("20-a.sql", "pending"), exists=False))
    present = cli.format_status(status(script("20-a.sql", "pending"), exists=True))

    assert "No migration record in this database yet" in absent and "safe" in absent and "adopted" in absent
    assert "No migration record" not in present


def test_the_result_lists_scripts_in_application_order_whichever_were_applied_and_ends_with_the_totals(monkeypatch):
    monkeypatch.setattr(migrate, "shipped", lambda: [migrate.Script("20-a.sql", "x"), migrate.Script("22-b.sql", "y")])

    text = cli.format_result(migrate.Result(applied=["22-b.sql"], skipped=["20-a.sql"]))

    assert text.splitlines() == ["current  20-a.sql", "applied  22-b.sql", "1 applied, 1 already current."]


# --- exit statuses ---------------------------------------------------------------------------------------------------------

def test_applying_exits_0_and_prints_the_result_on_stdout(no_database, monkeypatch, capsys):
    async def migrated(*, lock_timeout_seconds):
        migrated.seconds = lock_timeout_seconds
        return migrate.Result(["20-a.sql"], [])

    monkeypatch.setattr(migrate, "migrate", migrated)
    monkeypatch.setattr(migrate, "shipped", lambda: [migrate.Script("20-a.sql", "x")])

    code, out, err = run(["--lock-timeout-seconds", "7"], capsys)

    assert code == 0 and "1 applied, 0 already current." in out and err == ""
    assert migrated.seconds == 7.0, "the option reaches the library"


def test_status_exits_0_even_when_something_is_pending_and_writes_nothing(no_database, monkeypatch, capsys):
    async def never(**kwargs):
        raise AssertionError("--status must not migrate")

    async def pending():
        return status(script("20-a.sql", "pending"), exists=False)

    monkeypatch.setattr(migrate, "migrate", never)
    monkeypatch.setattr(migrate, "status", pending)

    code, out, _ = run(["--status"], capsys)

    assert code == 0 and "pending  20-a.sql" in out


@pytest.mark.parametrize(
    ("scripts", "unknown", "expected"),
    [
        ([script("20-a.sql"), script("22-b.sql")], [], 0),
        ([script("20-a.sql"), script("22-b.sql", "pending")], [], 1),
        ([script("20-a.sql", "changed", recorded="f" * 64), script("22-b.sql")], [], 1),
        ([script("20-a.sql"), script("22-b.sql")], ["99-z.sql"], 0),
        ([script("20-a.sql", "pending"), script("22-b.sql", "changed", recorded="f" * 64)], ["99-z.sql"], 1),
    ],
)
def test_check_exits_1_exactly_when_a_shipped_script_is_pending_or_changed(no_database, monkeypatch, capsys, scripts, unknown, expected):
    async def report():
        return status(*scripts, unknown=unknown)

    monkeypatch.setattr(migrate, "status", report)

    code, out, err = run(["--check"], capsys)

    assert code == expected
    assert out.strip() != "" and err == "", "the listing goes to stdout in both cases, nothing to stderr"


def test_a_migration_that_did_not_complete_exits_4_with_one_error_line_and_no_traceback(no_database, monkeypatch, capsys):
    async def times_out(*, lock_timeout_seconds):
        raise migrate.MigrationLockTimeout("20-credit-wallet.sql", lock_timeout_seconds)

    monkeypatch.setattr(migrate, "migrate", times_out)

    code, out, err = run(["--lock-timeout-seconds", "2"], capsys)

    assert code == 4 and out == ""
    assert err.strip().splitlines() == [
        "error: 20-credit-wallet.sql did not complete: could not get a lock within 2s (another transaction holds a table this script needs, "
        "or another migration is running). Nothing from this script was applied; running again is safe."
    ]


def test_a_failing_script_exits_4_naming_the_script_and_the_database_message(no_database, monkeypatch, capsys):
    async def fails(*, lock_timeout_seconds):
        raise migrate.MigrationFailed("22-billing.sql", "permission denied for schema public")

    monkeypatch.setattr(migrate, "migrate", fails)

    code, _, err = run([], capsys)

    assert code == 4 and err.startswith("error: 22-billing.sql did not complete: permission denied for schema public.")
    assert "Traceback" not in err


def test_an_unreachable_database_exits_3_in_seconds_with_one_error_line_and_no_credentials(closed_port_url, fast_failure, monkeypatch, capsys):
    monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)

    code, out, err = run([], capsys)

    assert code == EXIT_DATABASE_UNAVAILABLE == 3 and out == ""
    assert err.startswith("error: cannot open a connection to 127.0.0.1:")
    assert "Traceback" not in err and SECRET not in err + out and "billing_user" not in err + out


@pytest.mark.parametrize("flags", [["--status"], ["--check"]])
def test_status_and_check_also_exit_3_when_the_database_is_down(closed_port_url, fast_failure, monkeypatch, capsys, flags):
    monkeypatch.setenv("APPDATA_DATABASE_URL", closed_port_url)

    code, _, err = run(flags, capsys)

    assert code == 3 and err.startswith("error:")
