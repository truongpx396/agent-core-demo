"""The model must have no path to a balance (constitution II, specs/010 plan).

Granting, adjusting or clawing back credits is an operator action or a signature-verified webhook,
never a tool call. A tool the model can choose is a tool a poisoned document can steer, so a write to
a wallet from one would be a write the approval gate exists to stop: the fix is that no such tool exists.

This is structural rather than a list of names: a tool cannot reach the wallet without importing it, so
no module that defines or serves agent tools may import `credit_billing` (the billing package) at all, nor `app.billing_integration`
(the host's side of the same boundary: it wires the wallet to the pool and holds the reconciliation and export that
read it; specs/011-billing-module-boundary). (`usage_events`, which
is not a tool module, is where the wallet is debited, and `budgets`, which is not one either, is where it
is read to gate a turn; both run in the app's own turn path, outside anything the model can call.)
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Everything that defines, wraps or serves a tool the model can call.
TOOL_MODULES = [
    *sorted((REPO / "app" / "domains").rglob("*.py")),
    *sorted((REPO / "app" / "mcp").rglob("*.py")),
    REPO / "app" / "agent" / "tools.py",
    REPO / "app" / "agent" / "subagent_tools.py",
    REPO / "app" / "agent" / "subagent_domain_tools.py",
    REPO / "app" / "agent" / "tool_idempotency.py",
]


def _is_wallet(module: str) -> bool:
    """`credit_billing` is the wallet (the package); `app.billing_integration` is the agent app's side of it. Neither may be
    reached from a tool. The dot matters: `credit_billingx` is not the package."""
    return any(module == name or module.startswith(name + ".") for name in ("credit_billing", "app.billing_integration"))


def _imports_billing(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import) and any(_is_wallet(a.name) for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (
            _is_wallet(node.module or "") or (node.module == "app" and any(a.name == "billing_integration" for a in node.names))
        ):
            return True
    return False


def test_no_module_that_serves_a_tool_imports_the_wallet():
    offenders = [p.relative_to(REPO).as_posix() for p in TOOL_MODULES if p.exists() and _imports_billing(p)]

    assert offenders == [], f"a tool module reaches the credit wallet: {offenders}"


def test_the_scan_actually_covers_the_tool_modules():
    """Guards the guard: an empty or misspelt list would pass vacuously."""
    covered = {p.relative_to(REPO).as_posix() for p in TOOL_MODULES if p.exists()}

    assert "app/agent/tools.py" in covered and "app/agent/subagent_tools.py" in covered
    assert any(name.startswith("app/domains/support/") for name in covered)


def test_the_scanner_does_recognise_an_import_of_the_wallet(tmp_path):
    sample = tmp_path / "bad_tool.py"
    for source in (
        "from credit_billing import credits",
        "import credit_billing.credits",
        "import credit_billing",
        "from credit_billing.port import WalletPort",
        "import app.billing_integration",
        "from app.billing_integration import export",
        "from app.billing_integration.reconcile import Report",
        "from app import billing_integration",
    ):
        sample.write_text(source + "\n")
        assert _imports_billing(sample), source
    for source in ("from app.agent import budget_holds", "from credit_billingx import y", "import app.billing", "from app import billing"):
        sample.write_text(source + "\n")
        assert not _imports_billing(sample), source  # `app.billing` no longer exists, so importing it is a bug elsewhere, not a wallet path
