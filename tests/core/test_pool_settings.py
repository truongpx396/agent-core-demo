"""Every connection this app opens is safe behind a transaction pooler, and the appdata pool's size is a setting (specs/022).

psycopg prepares a statement after `prepare_threshold` runs on one connection (default 5; `0` means on the FIRST run, only `None` means
never). A transaction pooler gives the next transaction a different server connection, where that statement does not exist. The checkpointer
pool said `0` and the appdata pool used the default; nothing ran through a pooler, so nothing noticed (ENGINEERING_PATTERNS.md 95).

`tests/integration/test_pgbouncer.py` proves the behaviour against a real PgBouncer. These are the cheap, always-on halves: the pools are built
with the right arguments, and a scan finds any connection site in `app/` or the billing package that does not say so, with a table of cases
for the scan itself (a scanner that has never been seen to flag anything proves nothing, pattern 88).
"""
import ast
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import sql_store
from app.core.config import Settings

REPO = Path(__file__).resolve().parents[2]
SCANNED = [*sorted((REPO / "app").rglob("*.py")), *sorted((REPO / "packages" / "billing" / "src").rglob("*.py"))]
POOL_CLASSES = {"AsyncConnectionPool", "ConnectionPool"}
# What a direct connect() is called on; `client.connect(host)` and the like are not database connections.
CONNECT_RECEIVERS = {"psycopg", "psycopg.AsyncConnection", "psycopg.Connection", "AsyncConnection", "Connection"}


def _is_connection_site(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in POOL_CLASSES
    if isinstance(func, ast.Attribute):
        return func.attr in POOL_CLASSES or (func.attr == "connect" and ast.unparse(func.value) in CONNECT_RECEIVERS)
    return False


PREPARE_THRESHOLD_VALUE = re.compile(r"prepare_threshold['\"]?\s*[:=]\s*(\w+)")


def sites_without_prepare_off(source: str) -> list[int]:
    """Line numbers of connection sites that neither set `prepare_threshold` to None nor spread the shared kwargs, or that set it to anything else."""
    tree = ast.parse(source)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_connection_site(node):
            text = ast.unparse(node)  # not the source segment: a comment inside the call that mentions the old value must not count
            values = PREPARE_THRESHOLD_VALUE.findall(text)
            says_off = "POOLER_SAFE_CONNECTION_KWARGS" in text or "None" in values
            if not says_off or any(value != "None" for value in values):
                bad.append(node.lineno)
    return bad


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("AsyncConnectionPool(url, min_size=1)", [1]),
        ("AsyncConnectionPool(url, kwargs={'prepare_threshold': None})", []),
        ("AsyncConnectionPool(url, kwargs=dict(POOLER_SAFE_CONNECTION_KWARGS))", []),
        ("AsyncConnectionPool(url, kwargs={'autocommit': True, **POOLER_SAFE_CONNECTION_KWARGS})", []),
        ("psycopg.AsyncConnection.connect(url, connect_timeout=2)", [1]),
        ("psycopg.AsyncConnection.connect(url, **POOLER_SAFE_CONNECTION_KWARGS)", []),
        ("AsyncConnectionPool(url, kwargs={'autocommit': True, 'prepare_threshold': 0})", [1]),  # 0 prepares on the FIRST run
        ("AsyncConnectionPool(url, kwargs={'prepare_threshold': 5})", [1]),
        ("psycopg.connect(url, prepare_threshold=0)", [1]),
        ("psycopg.connect(url)", [1]),
        ("conn = psycopg.connect(url, prepare_threshold=None)", []),
        ("client.connect(host)", []),  # an unrelated .connect() is not a database connection
        ("x = 1\n\nAsyncConnectionPool(url)", [3]),
    ],
)
def test_the_scan_flags_exactly_the_connection_sites_that_do_not_turn_prepared_statements_off(source, expected):
    assert sites_without_prepare_off(source) == expected


def test_every_connection_site_in_the_app_and_the_billing_package_turns_prepared_statements_off():
    found: dict[str, list[int]] = {}
    flagged = {}
    for path in SCANNED:
        source = path.read_text()
        if "ConnectionPool" in source or ".connect(" in source:
            bad = sites_without_prepare_off(source)
            if bad:
                flagged[str(path.relative_to(REPO))] = bad
            found[str(path.relative_to(REPO))] = bad
    # Not vacuous: the four sites this was written for are all in the scan.
    for expected in ("app/core/sql_store.py", "app/adapters/outbound/postgres/checkpointing.py", "app/adapters/inbound/api/health.py", "packages/billing/src/credit_billing/cli/_runtime.py"):
        assert expected in found, f"{expected} is not scanned"
    assert not flagged, f"connection sites that would break behind a transaction pooler (pass prepare_threshold=None): {flagged}"


def test_the_appdata_pool_size_defaults_to_the_old_literal_and_must_be_positive():
    assert Settings().appdata_pool_max_size == 10
    with pytest.raises(ValidationError):
        Settings(appdata_pool_max_size=0)


class _FakePool:
    seen: dict = {}

    def __init__(self, conninfo, **kwargs):
        _FakePool.seen = {"conninfo": conninfo, **kwargs}

    async def open(self, **_):
        pass


async def test_the_appdata_pool_is_sized_by_the_setting_and_does_not_prepare_statements(monkeypatch):
    monkeypatch.setattr(sql_store, "AsyncConnectionPool", _FakePool)
    monkeypatch.setattr(sql_store, "APPDATA_POOL_MAX_SIZE", 7)
    monkeypatch.setattr(sql_store, "_pool", None)

    await sql_store._get_pool()

    assert _FakePool.seen["max_size"] == 7
    assert _FakePool.seen["kwargs"] == {"prepare_threshold": None}


async def test_the_checkpointer_pool_does_not_prepare_statements_and_keeps_its_other_settings(monkeypatch):
    from app.adapters.outbound.postgres import checkpointing

    class FakeSaver:
        def __init__(self, conn):
            self.conn = conn

        async def setup(self):
            pass

    monkeypatch.setattr(checkpointing, "AsyncConnectionPool", _FakePool)
    monkeypatch.setattr(checkpointing, "AsyncPostgresSaver", FakeSaver)

    await checkpointing.PgCheckpointing().open()

    kwargs = _FakePool.seen["kwargs"]
    assert kwargs["prepare_threshold"] is None, "0 means 'prepare on the first run', the opposite of what a transaction pooler needs"
    assert kwargs["autocommit"] is True and "row_factory" in kwargs
