"""The billing package depends on nothing it does not declare (specs/011-billing-module-boundary D26). This is
the PACKAGE's half of the boundary and knows nothing of the repository around it: the rule isn't "don't import
the agent app" (true of one host only) but that every module under `src/credit_billing` imports only the
standard library, itself and the dependencies its own `pyproject.toml` declares. So it can't depend on ANY
host, and a new import of an undeclared library fails here until declared on purpose.

It walks EVERY import node, nested ones included: the two provider adapters once imported the app's settings
inside a constructor, which a top-level scan misses. The repo's tests/billing/test_billing_core_boundary.py
enforces the same line from the app's side. Each rule has a test of the scanner itself, because a guard that
can't fail proves nothing. The behaviour tests at the bottom (an unwired package refuses; the connection hands
the provider's own transaction to the caller; the settings stand alone) moved here since they need only the
package.
"""
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from credit_billing import db as billing_db

PACKAGE = Path(__file__).resolve().parents[1]
SRC = PACKAGE / "src"
CORE = SRC / "credit_billing"

# The import names each declared distribution provides. A dependency added to pyproject.toml without an entry here fails the first
# test below, so adding one is a conscious change to this list too.
IMPORT_NAMES = {
    "psycopg": {"psycopg", "psycopg_pool"},
    "pydantic": {"pydantic"},
    "pydantic-settings": {"pydantic_settings"},
    "httpx": {"httpx"},
    "opentelemetry-api": {"opentelemetry"},
    "opentelemetry-sdk": {"opentelemetry"},
    "opentelemetry-exporter-otlp-proto-http": {"opentelemetry"},
    "fastapi": {"fastapi"},
}


def _declared() -> list[str]:
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())["project"]
    requirements = [*project["dependencies"], *project["optional-dependencies"]["http"], *project["optional-dependencies"]["otel"]]
    return [re.split(r"[\[<>=!~ ;]", requirement)[0].lower() for requirement in requirements]


def _package_of(path: Path) -> str:
    return ".".join(path.relative_to(SRC).with_suffix("").parts[:-1])


def _imported(source: str, package: str) -> list[tuple[int, str]]:
    """(line, absolute dotted name) for every thing any import statement names, wherever it is (module level, inside a function or a
    class); a relative import is resolved against `package` first."""
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


ALLOWED = set(sys.stdlib_module_names) | {"credit_billing"} | {name for dist in _declared() for name in IMPORT_NAMES.get(dist, ())}


def _undeclared(source: str, package: str) -> list[tuple[int, str]]:
    return [(line, name) for line, name in _imported(source, package) if name.split(".")[0] not in ALLOWED]


CORE_FILES = sorted(CORE.rglob("*.py"))


def test_every_declared_dependency_has_a_known_import_name():
    unknown = [dist for dist in _declared() if dist not in IMPORT_NAMES]

    assert unknown == [], f"add the import names of {unknown} to IMPORT_NAMES: this list is what the next test checks against"


def test_the_package_imports_only_the_standard_library_itself_and_what_it_declares():
    offenders = [
        f"{path.relative_to(SRC)}:{line}  {name}"
        for path in CORE_FILES
        for line, name in _undeclared(path.read_text(encoding="utf-8"), _package_of(path))
    ]

    assert offenders == [], "the package imports something it does not declare (the application that hosts it, or a library):\n  " + "\n  ".join(offenders)


def test_the_scan_actually_covers_the_package():
    """Guards the guard: an empty or misspelt directory would pass vacuously."""
    covered = {path.relative_to(CORE).as_posix() for path in CORE_FILES}

    assert {"credits.py", "webhooks.py", "inbox.py", "db.py", "settings.py", "metrics.py", "http.py", "port.py", "schema.py",
            "providers/stripe.py", "providers/polar.py", "cli/credits.py", "cli/_runtime.py"} <= covered


@pytest.mark.parametrize(
    "source",
    [
        "import app",
        "from app import core",
        "from app.core.config import STRIPE_API_KEY",
        "import requests",
        "def f():\n    import yaml\n",
        "class C:\n    def m(self):\n        from sqlalchemy import text\n",
        "import credit_billingx",  # the prefix trap: not the package
        "from credit_billingx import y",
    ],
)
def test_the_scanner_recognises_an_undeclared_import_wherever_it_hides(source):
    assert _undeclared(source, "credit_billing.providers"), source


@pytest.mark.parametrize(
    "source",
    [
        "import os, json\nfrom decimal import Decimal\nfrom collections.abc import Mapping",
        "from psycopg_pool import AsyncConnectionPool",
        "from psycopg import AsyncConnection",
        "from opentelemetry import metrics",
        "from pydantic_settings import BaseSettings",
        "import httpx",
        "from . import base",
        "from .. import credits",
        "from credit_billing import db",
        "def f():\n    from opentelemetry.sdk.metrics import MeterProvider\n",
    ],
)
def test_the_scanner_accepts_what_the_package_may_import(source):
    assert _undeclared(source, "credit_billing.providers") == [], source


def test_importing_every_module_in_a_clean_interpreter_never_needs_the_application(tmp_path):
    """The runtime half. A clean interpreter in an empty directory whose import path is `src` and nothing else, in which the
    application is asserted NOT importable, imports every module and reports any `app.*` module that got loaded."""
    modules = sorted(".".join(path.relative_to(SRC).with_suffix("").parts) for path in CORE_FILES if path.name != "__init__.py")
    code = (
        "import importlib, importlib.util, json, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "assert importlib.util.find_spec('app') is None, 'the application is importable here, so this test proves nothing'\n"
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


def test_the_settings_stand_alone_and_never_echo_a_rejected_value(tmp_path):
    """`BillingSettings` is built at import of `credit_billing.settings`, so a bad value fails the import. It must do that without
    anything of the host being imported, and without repeating the value it rejected."""
    publishable = "pk" + "_test_" + "x" * 16  # assembled at run time: a literal key-shaped string trips the repo's secret scan
    base = f"import sys; sys.path.insert(0, {str(SRC)!r})\n"

    ok = subprocess.run(
        [sys.executable, "-I", "-c", base + "import credit_billing.settings as s; assert not any(m == 'app' or m.startswith('app.') for m in sys.modules); print('ok')"],
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")}, capture_output=True, text=True, timeout=60,
    )
    assert ok.returncode == 0 and ok.stdout.strip() == "ok", ok.stderr[-2000:]

    bad = subprocess.run(
        [sys.executable, "-I", "-c", base + "import credit_billing.settings"],
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


async def test_an_unconfigured_package_refuses_instead_of_guessing(monkeypatch):
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
