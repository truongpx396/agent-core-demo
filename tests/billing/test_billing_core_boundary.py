"""The agent app's half of the billing boundary (specs/011-billing-module-boundary). Billing is a package
(`packages/billing/`, `credit_billing`) that the app uses and that must not know the app exists. Two test
files enforce it, one per side:
- `packages/billing/tests/test_boundary.py` is the PACKAGE's half and knows nothing of this repo: it imports
  only the stdlib, itself and the dependencies its own `pyproject.toml` declares.
- this file is the APP's half. It also keeps the repo-level gate that the package never reaches into `app.*`
  (a belt to the package's braces, failing first in this repo's run), and everything about how the APP reaches
  the package.

Each rule has a test of the scanner itself, because a guard that cannot fail proves nothing. Seven things are
enforced:
1. no file in the package imports `app` or `app.<anything>` (AST over every import node, nested ones too: the
   provider adapters once imported the app's settings inside a constructor, which a top-level scan misses);
2. importing every package module with the app NOT on the path loads no `app.*` module (runtime; catches
   imports reached through `importlib`);
3. every host module importing a database-using package module also imports `app.billing_integration`, whose
   import wires the package to the app's pool, so no entry point reaches it unwired;
4. nothing under `app/` or `scripts/` outside `app/billing_integration/` imports `credit_billing`, except
   `app/core/config.py` for its settings module (the app's `Settings` inherits `BillingSettings`);
5. no host file outside `app/billing_integration/` holds an SQL string naming a package table (an import scan
   can't see `SELECT ... FROM billing_customers`, which is how the app used to reach in);
6. the old `app/billing/` directory is gone, so nothing imports a stale copy;
7. (spec 013) the agent core carries no billing VOCABULARY: no identifier or string outside
   `app/billing_integration/` names a rate, a markup, the gate's or export's settings, or a package table
   (import and SQL scans can't see a setting name or a column, which is how the agent's meter once carried
   rating, debit and export).
"""
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "packages" / "billing" / "src"
CORE = SRC / "credit_billing"


def _package_of(path: Path) -> str:
    """`app/agent/cost/budgets.py` -> `app.agent`; `.../src/credit_billing/providers/stripe.py` -> `credit_billing.providers`
    (the package a relative import in that file is resolved against)."""
    try:
        relative = path.relative_to(SRC)
    except ValueError:
        relative = path.relative_to(REPO)
    return ".".join(relative.with_suffix("").parts[:-1])


def _imported(source: str, package: str) -> list[tuple[int, str]]:
    """(line, absolute dotted name) for every thing any import statement in `source` names, wherever the
    statement is (module level, inside a function, inside a class).
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([module] if module else []))
            found.extend((node.lineno, f"{module}.{alias.name}" if alias.name != "*" else module) for alias in node.names)
    return found


def _is_core(name: str) -> bool:
    return name == "credit_billing" or name.startswith("credit_billing.")  # the dot matters: `credit_billingx` is not the package


# The two package modules that never touch the database. A host module that imports only these (app/core/config.py imports the
# settings, and could not import the wiring anyway: sql_store imports config, so it would be a cycle) has nothing to wire.
_NEEDS_NO_CONNECTION = ("credit_billing.settings", "credit_billing.metrics")


def _uses_the_core_database(name: str) -> bool:
    return _is_core(name) and not any(name == exempt or name.startswith(exempt + ".") for exempt in _NEEDS_NO_CONNECTION)


def _is_integration(name: str) -> bool:
    return name == "app.billing_integration" or name.startswith("app.billing_integration.")


def _is_host(name: str) -> bool:
    return name == "app" or name.startswith("app.")  # the package may not import ANY of it, the integration package included


def _host_imports_in(source: str, package: str) -> list[tuple[int, str]]:
    return [(line, name) for line, name in _imported(source, package) if _is_host(name)]


CORE_FILES = sorted(CORE.rglob("*.py"))


def test_the_billing_package_imports_nothing_from_the_host():
    offenders = [
        f"{path.relative_to(REPO)}:{line}  {name}"
        for path in CORE_FILES
        for line, name in _host_imports_in(path.read_text(encoding="utf-8"), _package_of(path))
    ]

    assert offenders == [], "the billing package reaches back into the agent app:\n  " + "\n  ".join(offenders)


def test_the_scan_actually_covers_the_package():
    """Guards the guard: an empty or misspelt directory would pass vacuously."""
    covered = {path.relative_to(CORE).as_posix() for path in CORE_FILES}

    expected = {
        "credits.py", "webhooks.py", "inbox.py", "store.py", "db.py", "settings.py", "metrics.py", "http.py", "port.py", "schema.py",
        "providers/stripe.py", "providers/polar.py", "providers/base.py", "cli/credits.py", "cli/inbox_sweep.py", "cli/_runtime.py",
    }
    assert expected <= covered, f"the scan does not see: {sorted(expected - covered)}"


@pytest.mark.parametrize(
    "source",
    [
        "import app",
        "import app.core.config",
        "from app.core import metrics",
        "from app.core.config import STRIPE_API_KEY",
        "from app import core",
        "from app.core.sql_store import get_connection",
        "def f():\n    from app.core.config import X\n",
        "class C:\n    def m(self):\n        if True:\n            import app.core.gateway\n",
        "from app.billingx import y",
        "import app.billingx",
        "from app import billing_integration",  # the package may not import the host's side of the seam either
        "from app.billing_integration import port",
    ],
)
def test_the_scanner_recognises_every_way_to_reach_the_host(source):
    assert _host_imports_in(source, "credit_billing.providers"), source


@pytest.mark.parametrize(
    "source",
    [
        "from credit_billing import credits",
        "import credit_billing.credits",
        "from credit_billing.providers.base import BillingEvent",
        "from . import base",
        "from .base import BillingEvent",
        "from .. import credits",
        "import os\nimport psycopg\nfrom decimal import Decimal",
        "from psycopg import AsyncConnection",
    ],
)
def test_the_scanner_accepts_what_the_package_may_import(source):
    assert _host_imports_in(source, "credit_billing.providers") == [], source


def test_importing_every_package_module_with_the_app_off_the_path_loads_no_host_module(tmp_path):
    """The runtime half."""
    modules = sorted(
        ".".join(path.relative_to(SRC).with_suffix("").parts) for path in CORE_FILES if path.name != "__init__.py"
    )
    code = (
        "import importlib, importlib.util, json, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "assert importlib.util.find_spec('app') is None, 'the host is importable, so this test proves nothing'\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))))\n"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_the_old_app_billing_directory_is_gone():
    assert not (REPO / "app" / "billing").exists(), "app/billing/ moved to packages/billing/; a leftover directory can shadow it"


# --- the host's side: every module that uses the package's database also wires it --------------------------------

def _needs_the_wiring_import(source: str, package: str) -> bool:
    names = [name for _, name in _imported(source, package)]
    return any(_uses_the_core_database(n) for n in names) and not any(_is_integration(n) for n in names)


def _host_files() -> list[Path]:
    """Everything the agent app and its scripts import from, except the integration package, which is the seam itself."""
    seam = REPO / "app" / "billing_integration"
    return sorted(path for root in (REPO / "app", REPO / "scripts") for path in root.rglob("*.py") if seam not in path.parents)


def test_every_host_module_that_uses_the_package_also_wires_it():
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in _host_files()
        if _needs_the_wiring_import(path.read_text(encoding="utf-8"), _package_of(path))
    ]

    assert offenders == [], (
        "these modules import credit_billing but not app.billing_integration, so a process that runs only them reaches the "
        f"package with no database connection provider: {offenders}"
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from credit_billing import credits", True),
        ("import credit_billing.credits", True),
        ("from credit_billing import credits\nimport app.billing_integration", False),
        ("from credit_billing import credits\nfrom app.billing_integration import reconcile", False),
        ("from credit_billing import credits\nfrom app import billing_integration", False),
        ("from credit_billing import credits\nimport app.billing_integration.export", False),
        ("from app.agent.cost import budgets", False),
        ("from credit_billing.settings import BILLING_PROVIDERS", False),  # settings never touch the database
        ("from credit_billing import metrics", False),
        ("from credit_billing import settings", False),
        ("from credit_billing import credits, metrics", True),  # one that does is enough
        ("import app.billing_integration", False),
        ("from credit_billingx import y", False),  # not the package
        ("from app.billing_integration import reconcile", False),  # the integration alone is not a use of the package
    ],
)
def test_the_wiring_scanner_flags_exactly_the_unwired_users(source, flagged):
    assert _needs_the_wiring_import(source, "app.agent") is flagged, source


def test_the_prefix_trap_is_not_the_package():
    assert _is_core("credit_billing") and _is_core("credit_billing.credits")
    assert not _is_core("credit_billingx") and not _is_core("app.billing")


# --- the seam: the agent app reaches the package only through app.billing_integration ----------------------------

# The one structural exception: the host's central `Settings` INHERITS the package's `BillingSettings` (so every billing variable is
# defined once and the app's one settings object still holds it) and re-exports its constants. That is the only file that may
# import the package, and only its settings module: it may not reach `credits` or anything else.
_MAY_IMPORT_ONLY = {"app/core/config.py": "credit_billing.settings"}


def _seam_files() -> list[Path]:
    return _host_files()


def _allowed_direct_import(path: Path, name: str) -> bool:
    only = _MAY_IMPORT_ONLY.get(path.relative_to(REPO).as_posix())
    return only is not None and (name == only or name.startswith(only + "."))


def test_nothing_outside_the_seam_imports_the_billing_package():
    offenders = [
        f"{path.relative_to(REPO)}:{line}  {name}"
        for path in _seam_files()
        for line, name in _imported(path.read_text(encoding="utf-8"), _package_of(path))
        if _is_core(name) and not _allowed_direct_import(path, name)
    ]

    assert offenders == [], (
        "these files import credit_billing directly; the agent app reaches the wallet through app.billing_integration "
        "(app/billing_integration/port.py for the wallet, http.py for the webhook route):\n  " + "\n  ".join(offenders)
    )


def test_the_one_settings_exception_is_exactly_that_narrow():
    """`app/core/config.py` may import the package's settings module and nothing else of the package."""
    config = REPO / "app" / "core" / "config.py"

    assert _allowed_direct_import(config, "credit_billing.settings.STRIPE_API_KEY")
    assert _allowed_direct_import(config, "credit_billing.settings")
    assert not _allowed_direct_import(config, "credit_billing.credits")
    assert not _allowed_direct_import(config, "credit_billing.settingsx")  # the prefix trap
    assert not _allowed_direct_import(REPO / "app" / "agent" / "cost" / "usage_events.py", "credit_billing.settings")


def test_the_seam_scan_covers_the_files_that_used_to_import_the_package():
    covered = {path.relative_to(REPO).as_posix() for path in _seam_files()}

    assert {"app/agent/cost/usage_events.py", "app/agent/cost/budgets.py", "app/api/routers/usage.py", "app/api/main.py",
            "app/core/config.py", "scripts/usage_reconcile.py", "app/agent/cost/usage_reconcile.py"} <= covered
    assert all(not path.startswith("app/billing_integration/") for path in covered), "the seam itself is not scanned"
    users = {p for p in covered if any(_is_core(n) for _, n in _imported((REPO / p).read_text(encoding="utf-8"), _package_of(REPO / p)))}
    assert users == {"app/core/config.py"}, f"the only direct user of the package outside the seam is the settings inheritance ({sorted(users)})"


@pytest.mark.parametrize(
    "source",
    ["from credit_billing import credits", "import credit_billing.credits", "from credit_billing.port import BillingPort"],
)
def test_the_seam_rule_recognises_a_direct_import_of_the_package(source):
    assert any(_is_core(name) for _, name in _imported(source, "app.agent")), source


@pytest.mark.parametrize(
    "source",
    ["from app.billing_integration import port", "import app.billing_integration.export", "from app import billing_integration",
     "from app.core.config import STRIPE_API_KEY", "from credit_billingx import y"],
)
def test_the_seam_rule_allows_the_integration_package_and_the_hosts_own_config(source):
    assert not any(_is_core(name) for _, name in _imported(source, "app.agent")), source


# --- SQL that names a package table -----------------------------------------------------------------------------

_CORE_TABLES = (
    "billing_customers", "credit_accounts", "credit_lots", "credit_transactions", "credit_entries",
    "credit_products", "billing_webhook_events", "billing_usage_events", "billing_export_outbox",
)
_TABLE = re.compile(r"\b(" + "|".join(_CORE_TABLES) + r")\b")
# Uppercase on purpose: every statement in this repo is written that way, and a lowercase "from" next to a table name in an
# error message or a log line is prose, not SQL.
_SQL_KEYWORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|JOIN|INTO)\b")


def _sql_naming_a_core_table(source: str) -> list[tuple[int, str]]:
    """(line, table) for every string literal (an f-string counts as one, docstrings excluded) that holds both a package table
    name and an SQL keyword. Comments are not in the AST, so they are never scanned."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    inside_an_fstring = {id(child) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for child in ast.walk(node) if child is not node}
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in inside_an_fstring:
            continue
        if isinstance(node, ast.JoinedStr):
            text = "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            text = node.value
        else:
            continue
        table = _TABLE.search(text)
        if table and _SQL_KEYWORD.search(text):
            hits.append((node.lineno, table.group(1)))
    return hits


def test_no_host_file_outside_the_seam_holds_sql_that_names_a_package_table():
    offenders = [
        f"{path.relative_to(REPO)}:{line}  {table}"
        for path in _seam_files()
        for line, table in _sql_naming_a_core_table(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        "a host file reads or writes a package table with raw SQL; put the statement in app/billing_integration/ (the one host "
        "place allowed to name a package table) or behind the port:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        'q = "SELECT 1 FROM billing_customers"',
        'q = f"INSERT INTO credit_lots (id) VALUES ({x})"',
        'q = ("DELETE FROM " "credit_entries WHERE 1 = 1")',  # adjacent literals are one string
        'await conn.execute("UPDATE credit_accounts SET created_by = %s", (who,))',
        'def f():\n    return "SELECT * FROM credit_products WHERE active"',
    ],
)
def test_the_sql_scan_recognises_a_statement_that_names_a_package_table(source):
    assert _sql_naming_a_core_table(source), source


@pytest.mark.parametrize(
    "source",
    [
        '"""SELECT * FROM billing_customers is described here."""\nx = 1',  # a module docstring
        'def f():\n    """Reads credit_lots: SELECT ... FROM credit_lots."""\n    return 1',  # a function docstring
        'class C:\n    """FROM credit_entries"""\n    x = 1',
        '# SELECT * FROM credit_lots\nx = 1',  # a comment
        'log = "credit_lots drifted"',  # a table name with no SQL keyword
        'q = "SELECT 1 FROM usage_events"',  # a host table
        'log = "refused: nothing in credit_accounts, from the gate"',  # lowercase prose
    ],
)
def test_the_sql_scan_ignores_prose_docstrings_comments_and_host_tables(source):
    assert _sql_naming_a_core_table(source) == [], source


# --- the agent core carries no billing vocabulary (spec 013) ---------------------------------------------------------

# Names a billing concern owns. Matched against identifiers, attribute names, keyword arguments and string literals (docstrings and comments are
# prose and are not scanned): the rate and markup, the gate's and the export's settings, rating, and every table the package owns.
_BILLING_VOCABULARY = re.compile(
    r"(?i)\b(credits_per_usd|markup|credits_enforcement|credit_check_failure_policy|billing_export\w*|credits_for_cost|usage_export\w*|"
    + "|".join(_CORE_TABLES)
    + r")\b"
)


def _billing_words_in(source: str) -> list[tuple[int, str]]:
    """(line, word) for every identifier, attribute, keyword argument or non-docstring string literal that names a billing concern."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            text = node.id
        elif isinstance(node, ast.Attribute):
            text = node.attr
        elif isinstance(node, ast.keyword) and node.arg:
            text = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = node.name
        elif isinstance(node, ast.arg):
            text = node.arg
        elif isinstance(node, ast.alias):
            text = node.name.split(".")[-1]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            text = node.value
        else:
            continue
        match = _BILLING_VOCABULARY.search(text)
        if match:
            hits.append((getattr(node, "lineno", 0), match.group(1)))
    return hits


def test_the_agent_core_names_no_rate_markup_gate_setting_export_or_package_table():
    offenders = [
        f"{path.relative_to(REPO)}:{line}  {word}"
        for path in _seam_files()
        for line, word in _billing_words_in(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        "billing vocabulary leaked back into the agent core; it belongs to the package (spec 013), reached through "
        "app/billing_integration/port.py:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        "x = CREDITS_PER_USD",
        "from app.core.config import MARKUP",
        "def f(credits_per_usd): ...",
        "row = {'credits_per_usd': 1}",
        "q = 'INSERT INTO usage_export_outbox VALUES (1)'",
        "settings.billing_export_batch_size",
        "billing.credits_for_cost(1, 2, 3)",
        "f(markup=1)",
        "q = 'SELECT 1 FROM billing_usage_events'",
    ],
)
def test_the_vocabulary_scan_recognises_each_way_a_billing_word_gets_into_code(source):
    assert _billing_words_in(source), source


@pytest.mark.parametrize(
    "source",
    [
        '"""Rated with CREDITS_PER_USD and the MARKUP, by the billing package."""\nx = 1',  # a module docstring
        "def f():\n    \"\"\"Uses markup.\"\"\"\n    return 1",  # a function docstring
        "# CREDITS_PER_USD is the package's\nx = 1",  # a comment
        "code = 'insufficient_credits'",  # the API's refusal vocabulary is the contract with clients and stays
        "available_credits = 1",
        "from app.billing_integration import port as billing_port",
    ],
)
def test_the_vocabulary_scan_ignores_prose_and_the_apis_own_refusal_vocabulary(source):
    assert _billing_words_in(source) == [], source
