"""No SQL in the pooled processes keeps state on a database session (constitution Principle I; specs/022).

Behind a transaction pooler a server connection is lent to one transaction and then to a different client's. Anything that lives on the
SESSION outlives the transaction that set it and is seen by whoever is lent the connection next: a `SET` that was not `SET LOCAL`, a
`set_config(..., false)`, a session advisory lock, a `LISTEN`, a prepared statement, a temp table. With one tenant per request, that is one
tenant's setting (or lock, or notification) leaking into another tenant's query. This repo uses only transaction-local state
(`set_config(..., true)` for the retention switch, `pg_advisory_xact_lock` for the migration), so the pooler is safe; this test keeps it so.

It reads the SQL strings of `app/` and the billing package (the processes behind the pooler). It is a scan, not a proof: SQL assembled from
pieces it cannot see through is not covered, and `tests/integration/test_pgbouncer.py` runs the transaction-local pattern through a real
pooler. The scan has its own table of cases, so it has been seen to flag what it claims to.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNED = [*sorted((REPO / "app").rglob("*.py")), *sorted((REPO / "packages" / "billing" / "src").rglob("*.py"))]

# Statements that start a string (SQL here is upper case; prose that starts with a lower-case word never matches).
SESSION_STATEMENT = re.compile(r"^\s*(LISTEN|UNLISTEN|NOTIFY|PREPARE|DEALLOCATE|DISCARD|DECLARE|CREATE\s+(GLOBAL\s+|LOCAL\s+)?TEMP(ORARY)?)\b")
# `SET name = ...` / `SET SESSION name TO ...` / `SET ROLE` / `SET TIME ZONE` as a statement. `SET LOCAL ...` and an UPDATE's SET clause are not
# (neither starts the string), and prose that begins with "Set the gauge ..." is not an assignment.
SESSION_SET = re.compile(r"^\s*SET\s+(SESSION\s+)?([\w.]+\s*(=|\bTO\b)|TIME\s+ZONE\b|ROLE\b|AUTHORIZATION\b)", re.IGNORECASE)
# Session-level advisory locks; the `_xact_` variants are released with the transaction and are fine.
SESSION_LOCK = re.compile(r"\bpg_(try_)?advisory_(lock|unlock)(_shared|_all)?\s*\(")
# set_config(name, value, is_local) with is_local false: session scope.
SESSION_SET_CONFIG = re.compile(r"\bset_config\s*\([^)]*,\s*(false|'f'|'false')\s*\)", re.IGNORECASE)


def _strings(source: str):
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):  # an f-string: its literal parts are enough to see the statement
            yield node.lineno, "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))


def session_state_in(source: str) -> list[tuple[int, str]]:
    found = []
    for line, text in _strings(source):
        for pattern in (SESSION_STATEMENT, SESSION_SET, SESSION_LOCK, SESSION_SET_CONFIG):
            if pattern.search(text):
                found.append((line, text.strip().splitlines()[0][:60]))
                break
    return found


@pytest.mark.parametrize(
    ("sql", "flagged"),
    [
        ("SET statement_timeout = 5000", True),
        ("SET SESSION search_path = x", True),
        ("SET search_path TO public", True),
        ("SET ROLE admin", True),
        ("SET TIME ZONE 'UTC'", True),
        ("Set the gauge to the mode in force", False),
        ("SET LOCAL lock_timeout = '1000ms'", False),
        ("SET LOCAL statement_timeout = 5000", False),
        ("UPDATE crm_leads SET status = %s WHERE tenant = %s", False),
        ("INSERT INTO t VALUES (1) ON CONFLICT (a) DO UPDATE SET b = 2", False),
        ("SELECT set_config('usage_events.allow_delete', 'on', true)", False),
        ("SELECT set_config('app.tenant', %s, false)", True),
        ("SELECT pg_advisory_xact_lock(%s, %s)", False),
        ("SELECT pg_advisory_lock(42)", True),
        ("SELECT pg_try_advisory_lock(42)", True),
        ("SELECT pg_advisory_unlock(42)", True),
        ("LISTEN new_turns", True),
        ("NOTIFY new_turns", True),
        ("PREPARE q AS SELECT 1", True),
        ("CREATE TEMP TABLE scratch (a int)", True),
        ("CREATE TEMPORARY TABLE scratch (a int)", True),
        ("CREATE TABLE IF NOT EXISTS t (a int)", False),
        ("DECLARE c CURSOR WITH HOLD FOR SELECT 1", True),
        ("Notify the team when it is done", False),
    ],
)
def test_the_scan_flags_session_state_and_only_that(sql, flagged):
    assert bool(session_state_in(f"q = {sql!r}")) is flagged


def test_the_scan_sees_through_an_f_string_but_only_its_literal_part():
    assert session_state_in("q = f\"SET LOCAL lock_timeout = '{ms}ms'\"") == []
    assert session_state_in("q = f\"SET lock_timeout = '{ms}ms'\"") != []


def test_no_sql_in_the_pooled_processes_keeps_state_on_the_session():
    assert len(SCANNED) > 100, "the scan found (almost) nothing to read"
    offenders = {}
    for path in SCANNED:
        found = session_state_in(path.read_text())
        if found:
            offenders[str(path.relative_to(REPO))] = found
    assert not offenders, f"session-level state is carried to the next tenant by a transaction pooler: {offenders}"
