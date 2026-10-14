"""The migration tool's logic, against a fake database (specs/012-billing-migrate-idempotency, FR-001 to FR-012).

What the database does with the statements (the locks, the DDL, the concurrency) is proven against a real Postgres in
`tests/integration/test_migrate_real_postgres.py`. These tests pin what this module asks of it: which statements, in which order, in which
transaction, and what it does with the answers. The fake models the one property the module depends on: the provider's `async with` is a
transaction, committed on a clean exit and rolled back when the block raises (`credit_billing/db.py`).
"""
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import errors as pg_errors

from credit_billing import migrate, schema

NAMES = ["20-credit-wallet.sql", "22-billing.sql"]
SHIPPED = {script.name: script.checksum for script in migrate.shipped()}
WHEN = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


def kind(sql: str) -> str:
    """Names a statement by what it is, so a test can assert the ORDER without matching whole SQL strings."""
    text = " ".join(sql.split())
    for prefix, name in (
        ("SET LOCAL lock_timeout", "lock_timeout"),
        ("SELECT pg_advisory_xact_lock", "advisory_lock"),
        ("CREATE TABLE IF NOT EXISTS billing_schema_migrations", "create_record_table"),
        ("SELECT checksum FROM billing_schema_migrations", "check"),
        ("INSERT INTO billing_schema_migrations", "record"),
        ("SELECT to_regclass", "table_exists"),
        ("SELECT script, checksum, applied_at", "read_records"),
    ):
        if text.startswith(prefix):
            return name
    return "script"


class FakeConnection:
    def __init__(self, database: "FakeDatabase") -> None:
        self.database = database
        self.ledger = None if database.ledger is None else dict(database.ledger)
        self.statements: list[tuple[str, tuple | None]] = []

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        what = kind(sql)
        if what in self.database.fail_on:
            raise self.database.fail_on[what]
        if what == "table_exists":
            return FakeCursor([(self.ledger is not None,)])
        if what == "read_records":
            return FakeCursor([(name, checksum, applied_at) for name, (checksum, applied_at) in (self.ledger or {}).items()])
        if what == "create_record_table" and self.ledger is None:
            self.ledger = {}
        if what == "check":
            row = (self.ledger or {}).get(params[0])
            return FakeCursor([(row[0],)] if row else [])
        if what == "record":
            self.ledger[params[0]] = (params[1], WHEN)
        return FakeCursor([])


class FakeDatabase:
    """`ledger` is the committed record table: None means the table does not exist yet."""

    def __init__(self, ledger=None, fail_on=None) -> None:
        self.ledger = ledger
        self.fail_on: dict[str, Exception] = fail_on or {}
        self.transactions: list[list[str]] = []  # per transaction: the statement kinds, then COMMIT or ROLLBACK
        self.connections = 0

    @asynccontextmanager
    async def connection(self):
        self.connections += 1
        conn = FakeConnection(self)
        try:
            yield conn
        except BaseException:
            self.transactions.append([kind(s) for s, _ in conn.statements] + ["ROLLBACK"])
            raise
        self.ledger = conn.ledger
        self.transactions.append([kind(s) for s, _ in conn.statements] + ["COMMIT"])
        self.last = conn


@pytest.fixture
def database(monkeypatch):
    def install(ledger=None, fail_on=None) -> FakeDatabase:
        fake = FakeDatabase(ledger, fail_on)
        monkeypatch.setattr(migrate, "get_connection", fake.connection)
        return fake

    return install


def current_ledger() -> dict:
    return {name: (checksum, WHEN) for name, checksum in SHIPPED.items()}


# --- what is shipped ---------------------------------------------------------------------------------------------------

def test_shipped_lists_the_two_scripts_in_order_with_the_sha256_of_their_bytes():
    scripts = migrate.shipped()

    assert [script.name for script in scripts] == NAMES
    for script in scripts:
        assert script.checksum == hashlib.sha256(schema.read_bytes(script.name)).hexdigest()
        assert len(script.checksum) == 64


def test_the_checksum_is_over_the_script_as_shipped_so_it_includes_the_connect_line():
    """It must equal `sha256sum` of the file (and of the host's byte-equal copy), not of the text the tool executes."""
    script = migrate.shipped()[0]

    assert script.checksum != hashlib.sha256(migrate.statements(script.name).encode()).hexdigest()


def test_statements_is_the_script_without_its_connect_line():
    assert migrate.statements("22-billing.sql") == schema.executable("22-billing.sql")
    assert "\\connect" not in migrate.statements("22-billing.sql")


def test_the_advisory_key_is_the_two_integer_form_that_cannot_collide_with_a_tenant_lock():
    assert len(migrate.LOCK_KEYS) == 2 and all(isinstance(key, int) and 0 < key < 2**31 for key in migrate.LOCK_KEYS)


# --- migrate(): the order inside one script's transaction ---------------------------------------------------------------

EXPECTED_ORDER = ["lock_timeout", "advisory_lock", "create_record_table", "check", "script", "record", "COMMIT"]


async def test_a_pending_script_is_applied_in_its_own_transaction_in_exactly_this_order(database):
    fake = database(ledger=None)

    result = await migrate.migrate()

    assert result.applied == NAMES and result.skipped == []
    assert fake.transactions == [EXPECTED_ORDER, EXPECTED_ORDER], "one transaction per script, each with the same fixed order"
    assert fake.connections == 2
    assert set(fake.ledger) == set(NAMES)
    assert {name: checksum for name, (checksum, _) in fake.ledger.items()} == SHIPPED


async def test_the_script_that_runs_is_the_shipped_text_without_its_connect_line(database):
    fake = database(ledger=None)
    seen = []
    original = FakeConnection.execute

    async def capture(self, sql, params=None):
        if kind(sql) == "script":
            seen.append(sql)
        return await original(self, sql, params)

    FakeConnection.execute = capture
    try:
        await migrate.migrate()
    finally:
        FakeConnection.execute = original

    assert seen == [schema.executable(name) for name in NAMES]
    assert fake.ledger is not None


async def test_the_advisory_lock_is_taken_with_the_two_integer_keys_and_before_the_record_table_is_touched(database):
    fake = database(ledger=None)

    await migrate.migrate()

    kinds = fake.transactions[0]
    assert kinds.index("advisory_lock") < kinds.index("create_record_table") < kinds.index("check") < kinds.index("script")


async def test_the_lock_statement_is_the_two_integer_form_with_exactly_the_documented_keys(database):
    """The single-bigint form is the wallet's per-tenant key space; this one must stay out of it (research R3)."""
    fake = database(ledger=current_ledger())

    await migrate.migrate()

    locks = [(sql, params) for sql, params in fake.last.statements if kind(sql) == "advisory_lock"]
    assert locks == [("SELECT pg_advisory_xact_lock(%s, %s)", migrate.LOCK_KEYS)]


async def test_a_script_already_recorded_with_this_checksum_is_skipped_untouched(database):
    fake = database(ledger=current_ledger())

    result = await migrate.migrate()

    assert result.applied == [] and result.skipped == NAMES
    for kinds in fake.transactions:
        assert "script" not in kinds and "record" not in kinds, "no DDL, and applied_at is not rewritten"
    assert fake.ledger == current_ledger(), "every recorded time is exactly as it was"


async def test_a_script_whose_recorded_checksum_differs_is_applied_again_and_re_recorded(database):
    ledger = current_ledger()
    ledger["22-billing.sql"] = ("0" * 64, WHEN)
    fake = database(ledger=ledger)

    result = await migrate.migrate()

    assert result.applied == ["22-billing.sql"] and result.skipped == ["20-credit-wallet.sql"]
    assert fake.ledger["22-billing.sql"][0] == SHIPPED["22-billing.sql"]


async def test_the_lock_timeout_is_set_first_in_whole_milliseconds(database):
    fake = database(ledger=current_ledger())

    await migrate.migrate(lock_timeout_seconds=2.5)

    first = fake.last.statements[0]
    assert first[0] == "SET LOCAL lock_timeout = '2500ms'"


async def test_a_tiny_timeout_is_still_at_least_one_millisecond(database):
    fake = database(ledger=current_ledger())

    await migrate.migrate(lock_timeout_seconds=0.0001)

    assert fake.last.statements[0][0] == "SET LOCAL lock_timeout = '1ms'", "'0ms' would mean no timeout at all"


@pytest.mark.parametrize("bad", [0, -1, float("nan")])
async def test_a_timeout_that_is_not_above_zero_is_refused_before_any_connection(database, bad):
    fake = database(ledger=current_ledger())

    with pytest.raises(ValueError, match="above zero"):
        await migrate.migrate(lock_timeout_seconds=bad)

    assert fake.connections == 0


# --- migrate(): failures ---------------------------------------------------------------------------------------------------

async def test_a_lock_that_cannot_be_had_becomes_a_timeout_error_that_says_running_again_is_safe(database):
    fake = database(ledger=None, fail_on={"advisory_lock": pg_errors.LockNotAvailable("canceling statement due to lock timeout")})

    with pytest.raises(migrate.MigrationLockTimeout) as raised:
        await migrate.migrate(lock_timeout_seconds=3)

    message = str(raised.value)
    assert "20-credit-wallet.sql" in message and "3s" in message and "running again is safe" in message
    assert raised.value.script == "20-credit-wallet.sql" and raised.value.seconds == 3
    assert fake.transactions == [["lock_timeout", "advisory_lock", "ROLLBACK"]], "rolled back, never committed"
    assert fake.ledger is None, "nothing recorded"


async def test_a_lock_timeout_on_the_scripts_own_ddl_is_the_same_error(database):
    fake = database(ledger=None, fail_on={"script": pg_errors.LockNotAvailable("canceling statement due to lock timeout")})

    with pytest.raises(migrate.MigrationLockTimeout):
        await migrate.migrate()

    assert fake.transactions[0][-1] == "ROLLBACK" and fake.ledger is None


async def test_a_failing_script_rolls_back_whole_records_nothing_and_leaves_earlier_scripts_recorded(database):
    fake = database(ledger=None)
    original = FakeConnection.execute
    calls = {"scripts": 0}

    async def second_script_fails(self, sql, params=None):
        if kind(sql) == "script":
            calls["scripts"] += 1
            if calls["scripts"] == 2:
                self.statements.append((sql, params))
                raise pg_errors.DivisionByZero("division by zero\nDETAIL: more\nHINT: and more")
        return await original(self, sql, params)

    FakeConnection.execute = second_script_fails
    try:
        with pytest.raises(migrate.MigrationFailed) as raised:
            await migrate.migrate()
    finally:
        FakeConnection.execute = original

    assert raised.value.script == "22-billing.sql" and raised.value.detail == "division by zero", "only the first line of the database's message"
    assert str(raised.value).endswith("Nothing from this script was applied.")
    assert fake.transactions[0][-1] == "COMMIT" and fake.transactions[1][-1] == "ROLLBACK"
    assert set(fake.ledger) == {"20-credit-wallet.sql"}, "the first script stays recorded, the failed one does not"


async def test_a_connection_level_failure_never_names_the_user_or_the_password(database):
    leak = psycopg.OperationalError('connection to server at "db", port 5432 failed: FATAL: password authentication failed for user "billing_user"')
    database(ledger=None, fail_on={"script": leak})

    with pytest.raises(migrate.MigrationFailed) as raised:
        await migrate.migrate()

    assert "billing_user" not in str(raised.value) and "password" not in str(raised.value)
    assert raised.value.detail == "the database connection failed"


async def test_a_script_with_a_meta_command_the_tool_cannot_apply_fails_before_a_connection_is_taken(database, monkeypatch):
    fake = database(ledger=None)
    monkeypatch.setattr(migrate.schema, "read", lambda name: "\\connect appdata\n\\set x 1\nSELECT 1;\n")

    with pytest.raises(migrate.MigrationFailed, match=r"line 2.*meta-command"):
        await migrate.migrate()

    assert fake.connections == 0


async def test_an_error_raised_inside_the_block_reaches_the_provider_so_it_rolls_back(database):
    """The whole point of raising from INSIDE the `async with`: the provider's rollback only runs for an exception thrown into it."""
    fake = database(ledger=None, fail_on={"record": psycopg.errors.UniqueViolation("duplicate key")})

    with pytest.raises(migrate.MigrationFailed):
        await migrate.migrate()

    assert fake.transactions[0][-1] == "ROLLBACK" and fake.ledger is None


# --- status(): reads only ----------------------------------------------------------------------------------------------------

async def test_status_with_no_record_table_reports_everything_pending_and_creates_nothing(database):
    fake = database(ledger=None)

    status = await migrate.status()

    assert status.record_table_exists is False and status.up_to_date is False
    assert [(s.name, s.state, s.recorded_checksum, s.applied_at) for s in status.scripts] == [(n, "pending", None, None) for n in NAMES]
    assert fake.transactions == [["table_exists", "COMMIT"]], "one read, and the record table was not created"
    assert fake.ledger is None


async def test_status_never_executes_anything_but_a_select(database):
    fake = database(ledger=current_ledger())

    await migrate.status()

    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in fake.last.statements)


async def test_status_classifies_applied_changed_pending_and_unknown(database):
    ledger = {
        "20-credit-wallet.sql": (SHIPPED["20-credit-wallet.sql"], WHEN),
        "22-billing.sql": ("f" * 64, WHEN),
        "99-from-a-newer-version.sql": ("a" * 64, WHEN),
    }
    database(ledger=ledger)

    status = await migrate.status()

    assert [(s.name, s.state) for s in status.scripts] == [("20-credit-wallet.sql", "applied"), ("22-billing.sql", "changed")]
    assert status.scripts[0].applied_at == WHEN and status.scripts[1].recorded_checksum == "f" * 64
    assert status.scripts[1].checksum == SHIPPED["22-billing.sql"]
    assert status.unknown == ["99-from-a-newer-version.sql"]
    assert status.up_to_date is False


async def test_an_unknown_recorded_script_does_not_count_against_being_up_to_date(database):
    ledger = current_ledger() | {"99-from-a-newer-version.sql": ("a" * 64, WHEN)}
    database(ledger=ledger)

    status = await migrate.status()

    assert status.unknown == ["99-from-a-newer-version.sql"] and status.up_to_date is True


async def test_one_pending_script_means_not_up_to_date(database):
    database(ledger={"20-credit-wallet.sql": (SHIPPED["20-credit-wallet.sql"], WHEN)})

    status = await migrate.status()

    assert [s.state for s in status.scripts] == ["applied", "pending"] and status.up_to_date is False
