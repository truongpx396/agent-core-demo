"""The billing core must not depend on the agent app, and the agent app must wire it (specs/011-billing-module-boundary).

`app/billing/` is meant to become a package another project could install. It cannot be one while any file in it
imports the host (`app.agent`, `app.core`, `app.api`, ...), and nothing used to stop a new reach-back from being
added. Seven of its 13 files had one when this test was written, two of them lazily inside a constructor, where a
scan of top-level imports does not see them. That is why this walks EVERY import node, nested ones included.

Four things are enforced, and each has a test of the scanner itself, because a guard that cannot fail proves nothing
(the existing `test_the_wallet_is_not_reachable_by_the_model.py` is the model, and its third test is the same idea):

  1. no file under `app/billing/` imports `app` or `app.<anything>` except `app.billing[.<anything>]` (AST, all imports);
  2. importing every core module in a clean interpreter loads no `app.*` module outside `app.billing` (runtime; catches
     what an AST walk cannot, such as an import reached through `importlib`);
  3. the core refuses to run unwired, and its connection delegates the provider's own transaction;
  4. every host module that imports a core module which uses the database also imports `app.billing_integration`,
     the package whose import wires the core to the host's pool, so no entry point can reach the core unwired.
"""
import ast
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.billing import db as billing_db

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / "app" / "billing"


def _package_of(path: Path) -> str:
    """`app/billing/providers/stripe.py` -> `app.billing.providers` (the package a relative import is resolved against)."""
    return ".".join(path.relative_to(REPO).with_suffix("").parts[:-1])


def _imported(source: str, package: str) -> list[tuple[int, str]]:
    """(line, absolute dotted name) for every thing any import statement in `source` names, wherever the statement is
    (module level, inside a function, inside a class). `from app import core` yields `app.core`; `from app.core import
    metrics` yields `app.core.metrics`; a relative import is resolved against `package` first."""
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
    return name == "app.billing" or name.startswith("app.billing.")  # the dot matters: `app.billingx` is not the core


# The two core modules that never touch the database. A host module that imports only these (app/core/config.py imports
# the settings, and could not import the wiring anyway: sql_store imports config, so it would be a cycle) has nothing to wire.
_NEEDS_NO_CONNECTION = ("app.billing.settings", "app.billing.metrics")


def _uses_the_core_database(name: str) -> bool:
    return _is_core(name) and not any(name == exempt or name.startswith(exempt + ".") for exempt in _NEEDS_NO_CONNECTION)


def _is_integration(name: str) -> bool:
    return name == "app.billing_integration" or name.startswith("app.billing_integration.")


def _is_host(name: str) -> bool:
    return (name == "app" or name.startswith("app.")) and not _is_core(name)


def _host_imports_in(source: str, package: str) -> list[tuple[int, str]]:
    return [(line, name) for line, name in _imported(source, package) if _is_host(name)]


CORE_FILES = sorted(CORE.rglob("*.py"))


def test_the_billing_core_imports_nothing_from_the_host():
    offenders = [
        f"{path.relative_to(REPO)}:{line}  {name}"
        for path in CORE_FILES
        for line, name in _host_imports_in(path.read_text(encoding="utf-8"), _package_of(path))
    ]

    assert offenders == [], "the billing core reaches back into the agent app:\n  " + "\n  ".join(offenders)


def test_the_scan_actually_covers_the_core():
    """Guards the guard: an empty or misspelt directory would pass vacuously."""
    covered = {path.relative_to(CORE).as_posix() for path in CORE_FILES}

    expected = {
        "credits.py", "webhooks.py", "inbox.py", "store.py", "db.py", "settings.py", "metrics.py",
        "providers/stripe.py", "providers/polar.py", "providers/base.py",
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
        "from app.agent.sql_store import get_connection",
        "def f():\n    from app.core.config import X\n",
        "class C:\n    def m(self):\n        if True:\n            import app.agent.gateway\n",
        "from app.billingx import y",
        "import app.billingx",
        "from app import billing_integration",
        "from ... import core",  # three dots from app.billing.providers is `app`
    ],
)
def test_the_scanner_recognises_every_way_to_reach_the_host(source):
    assert _host_imports_in(source, "app.billing.providers"), source


@pytest.mark.parametrize(
    "source",
    [
        "from app.billing import credits",
        "import app.billing.credits",
        "from app.billing.providers.base import BillingEvent",
        "from app import billing",
        "from . import base",
        "from .base import BillingEvent",
        "from .. import credits",
        "import os\nimport psycopg\nfrom decimal import Decimal",
        "from psycopg import AsyncConnection",
    ],
)
def test_the_scanner_accepts_what_the_core_may_import(source):
    assert _host_imports_in(source, "app.billing.providers") == [], source


def test_importing_every_core_module_loads_no_host_module(tmp_path):
    """The runtime half. A clean interpreter in an empty directory (no `.env` to read, no ambient project config)
    imports every module under app/billing and reports any `app.*` module that got loaded outside `app.billing`."""
    modules = sorted(".".join(path.relative_to(REPO).with_suffix("").parts) for path in CORE_FILES if path.name != "__init__.py")
    code = (
        "import importlib, json, sys\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(m for m in sys.modules if (m == 'app' or m.startswith('app.')) "
        "and m != 'app' and not (m == 'app.billing' or m.startswith('app.billing.')))))\n"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")},
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_the_core_settings_stand_alone_and_never_echo_a_rejected_value(tmp_path):
    """`BillingSettings` is built at import of `app.billing.settings`, so a bad value fails the import. It must do that
    without `app.core.config` ever being imported, and without repeating the value it rejected."""
    publishable = "pk" + "_test_" + "x" * 16  # assembled at run time: a literal key-shaped string trips the repo's secret scan
    base = f"import sys; sys.path.insert(0, {str(REPO)!r})\n"

    ok = subprocess.run(
        [sys.executable, "-I", "-c", base + "import app.billing.settings as s; assert 'app.core.config' not in sys.modules; print('ok')"],
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, timeout=60,
    )
    assert ok.returncode == 0 and ok.stdout.strip() == "ok", ok.stderr[-2000:]

    bad = subprocess.run(
        [sys.executable, "-I", "-c", base + "import app.billing.settings"],
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", ""), "STRIPE_API_KEY": publishable},
        capture_output=True, text=True, timeout=60,
    )
    assert bad.returncode != 0, "a publishable Stripe key must be refused at startup"
    assert "STRIPE_API_KEY must be a secret" in bad.stderr
    assert publishable not in bad.stderr and publishable not in bad.stdout


# --- the connection: unset means refused, and the provider's own transaction is what the caller gets -------------

class _Provider:
    """Records what a real pooled connection would do: commit on a clean exit, roll back when the block raised."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def __call__(self):
        return self._connection()

    @asynccontextmanager
    async def _connection(self):
        self.events.append("enter")
        try:
            yield "the-connection"
        except BaseException as exc:
            self.events.append(f"rollback:{type(exc).__name__}")
            raise
        else:
            self.events.append("commit")


async def test_an_unconfigured_core_refuses_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(billing_db, "_provider", None)

    with pytest.raises(billing_db.BillingNotConfigured):
        async with billing_db.get_connection():
            pytest.fail("a connection was handed out with no provider configured")


async def test_a_clean_exit_reaches_the_provider_as_a_commit(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(billing_db, "_provider", provider)

    async with billing_db.get_connection() as conn:
        assert conn == "the-connection"

    assert provider.events == ["enter", "commit"]


async def test_an_exception_in_the_callers_block_reaches_the_provider_as_a_rollback(monkeypatch):
    provider = _Provider()
    monkeypatch.setattr(billing_db, "_provider", provider)

    with pytest.raises(ValueError, match="boom"):
        async with billing_db.get_connection():
            raise ValueError("boom")

    assert provider.events == ["enter", "rollback:ValueError"], "a caller's failure must not look like a commit"


def test_configure_replaces_and_clears_the_provider(monkeypatch):
    monkeypatch.setattr(billing_db, "_provider", None)
    provider = _Provider()

    billing_db.configure(connection=provider)
    assert billing_db._provider is provider
    billing_db.configure(connection=None)
    assert billing_db._provider is None


# --- the host's side: every module that imports the core also wires it ------------------------------------------

def _needs_the_wiring_import(source: str, package: str) -> bool:
    names = [name for _, name in _imported(source, package)]
    return any(_uses_the_core_database(n) for n in names) and not any(_is_integration(n) for n in names)


def _host_files() -> list[Path]:
    """Everything the agent app and its scripts import from, except the two packages that are the boundary itself."""
    skipped = (REPO / "app" / "billing", REPO / "app" / "billing_integration")
    return sorted(
        path for root in (REPO / "app", REPO / "scripts") for path in root.rglob("*.py")
        if not any(skip in path.parents for skip in skipped)
    )


def test_every_host_module_that_uses_the_core_also_wires_it():
    offenders = [
        path.relative_to(REPO).as_posix()
        for path in _host_files()
        if _needs_the_wiring_import(path.read_text(encoding="utf-8"), _package_of(path))
    ]

    assert offenders == [], (
        "these modules import app.billing but not app.billing_integration, so a process that runs only them reaches the "
        f"core with no database connection provider: {offenders}"
    )


def test_the_wiring_scan_covers_the_modules_that_use_the_core():
    covered = {path.relative_to(REPO).as_posix() for path in _host_files()}

    assert {"app/agent/usage_events.py", "app/agent/budgets.py", "app/api/routers/usage.py", "app/api/routers/billing.py",
            "scripts/credits.py", "scripts/billing_inbox_sweep.py"} <= covered
    users = [p for p in covered if "app.billing" in "\n".join(n for _, n in _imported((REPO / p).read_text(encoding="utf-8"), _package_of(REPO / p)))]
    assert len(users) >= 8, f"the wiring scan sees too few users of the core ({users}); it may be looking in the wrong place"


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from app.billing import credits", True),
        ("import app.billing.credits", True),
        ("from app import billing", True),
        ("from app.billing import credits\nimport app.billing_integration", False),
        ("from app.billing import credits\nfrom app.billing_integration import reconcile", False),
        ("from app.billing import credits\nfrom app import billing_integration", False),
        ("from app.billing import credits\nimport app.billing_integration.export", False),
        ("from app.agent import budgets", False),
        ("from app.billing.settings import BILLING_PROVIDERS", False),  # settings never touch the database
        ("from app.billing import metrics", False),
        ("from app.billing import settings", False),
        ("from app.billing import credits, metrics", True),  # one that does is enough
        ("import app.billing_integration", False),
        ("from app.billingx import y", False),  # not the core
        ("from app.billing_integration import reconcile", False),  # the integration alone is not a use of the core
    ],
)
def test_the_wiring_scanner_flags_exactly_the_unwired_users(source, flagged):
    assert _needs_the_wiring_import(source, "app.agent") is flagged, source
