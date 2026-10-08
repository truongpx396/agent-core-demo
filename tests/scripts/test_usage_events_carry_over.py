"""scripts/usage_events_carry_over.py: the control flow and the words the operator reads (specs/010 T030).

Hermetic, with a scripted connection. That proves the loop (when it stops, what it reports, what it hands the database); what
the statements actually DO to the tables is proven against a real Postgres in
tests/integration/test_usage_events_carry_over_real_postgres.py."""
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from psycopg import errors as pg_errors

from scripts import usage_events_carry_over as carry

CUTOFF = datetime(2026, 10, 7, 3, 21, tzinfo=UTC)


class _Cursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _Script:
    """Answers the cutoff query first, then each batch with the next scripted (last_id, scanned, inserted, usd)."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, sql, params):
        self.calls.append((sql, params))
        if "MIN(recorded_at)" in sql:
            return _Cursor((CUTOFF,))
        if "COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage_ledger" in sql:
            return _Cursor((7, Decimal("1.25")))
        return _Cursor(self.batches.pop(0))


def _use(monkeypatch, script):
    @asynccontextmanager
    async def get_connection():
        yield script

    monkeypatch.setattr(carry, "get_connection", get_connection)


async def test_it_stops_at_the_first_short_batch_and_reports_the_total(monkeypatch):
    script = _Script([(2, 2, 2, Decimal("0.5")), (4, 2, 2, Decimal("0.25")), (5, 1, 1, Decimal("0.125"))])
    _use(monkeypatch, script)

    result = await carry.run(batch_size=2)

    assert (result.carried, result.carried_usd, result.complete) == (5, Decimal("0.875"), True)
    assert result.cutoff == CUTOFF
    assert len(script.calls) == 1 + 3  # the cutoff, then one statement per batch


async def test_each_batch_resumes_after_the_last_id_of_the_one_before(monkeypatch):
    script = _Script([(40, 2, 2, Decimal(1)), (90, 2, 2, Decimal(1)), (None, 0, 0, Decimal(0))])
    _use(monkeypatch, script)

    await carry.run(batch_size=2)

    afters = [params["after"] for sql, params in script.calls if "WITH batch" in sql]
    assert afters == [0, 40, 90]


async def test_a_run_that_hits_its_ceiling_says_it_is_not_complete(monkeypatch):
    script = _Script([(2, 2, 2, Decimal(1)), (4, 2, 2, Decimal(1))])
    _use(monkeypatch, script)

    result = await carry.run(batch_size=2, max_batches=2)

    assert result.complete is False and result.carried == 4
    assert "run it again" in carry.describe(result)


async def test_an_empty_ledger_is_a_normal_result(monkeypatch):
    _use(monkeypatch, _Script([(None, 0, 0, Decimal(0))]))

    result = await carry.run()

    assert (result.carried, result.complete) == (0, True)
    assert carry.describe(result).startswith("Nothing to carry")


async def test_the_cutoff_is_the_first_real_event_and_never_a_carried_row(monkeypatch):
    script = _Script([(None, 0, 0, Decimal(0))])
    _use(monkeypatch, script)

    await carry.run()

    sql, params = script.calls[0]
    assert "NOT LIKE" in sql and params == {"like": "ledger:%"}
    assert "COALESCE(MIN(recorded_at), now())" in sql  # with no real event yet, the cutoff is now


async def test_a_dry_run_writes_nothing_and_says_what_it_would_do(monkeypatch):
    script = _Script([])
    _use(monkeypatch, script)

    result = await carry.run(dry_run=True)

    assert all("INSERT" not in sql for sql, _ in script.calls)
    assert (result.carried, result.carried_usd, result.dry_run) == (7, Decimal("1.25"), True)
    assert carry.describe(result).startswith("Would carry 7 ledger row(s) worth USD 1.25")


async def test_a_tenant_filter_is_handed_to_the_database_not_applied_in_python(monkeypatch):
    script = _Script([(None, 0, 0, Decimal(0))])
    _use(monkeypatch, script)

    await carry.run(tenant="acme")

    (batch_params,) = [params for sql, params in script.calls if "WITH batch" in sql]
    assert batch_params["tenant"] == "acme"


def test_it_names_what_it_carried_and_up_to_when():
    result = carry.CarryOver(CUTOFF, 1234, Decimal("12.345678"), True)

    assert carry.describe(result) == (
        "Carried 1234 ledger row(s) worth USD 12.345678, recorded before 2026-10-07T03:21:00+00:00."
    )


def test_a_missing_events_table_is_a_message_not_a_traceback(monkeypatch):
    @contextmanager
    def no_job(name):
        yield

    async def run(**kwargs):
        raise pg_errors.UndefinedTable('relation "usage_events" does not exist')

    monkeypatch.setattr(carry, "scheduled_job", no_job)
    monkeypatch.setattr(carry, "run", run)
    monkeypatch.setattr("sys.argv", ["usage_events_carry_over"])

    with pytest.raises(SystemExit) as exit_info:
        carry.main()

    assert "postgres-init/19-usage-events.sql" in str(exit_info.value)


async def test_the_statement_survives_a_second_run_racing_it(monkeypatch):
    """Shape only (the race itself is in the integration tier): the insert must tolerate a row another run just wrote."""
    script = _Script([(None, 0, 0, Decimal(0))])
    _use(monkeypatch, script)

    await carry.run()

    (batch_sql,) = [sql for sql, _ in script.calls if "WITH batch" in sql]
    assert "ON CONFLICT (event_id) DO NOTHING" in batch_sql
