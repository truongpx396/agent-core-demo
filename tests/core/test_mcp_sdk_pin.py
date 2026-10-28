"""The MCP SDK generation this repo runs on, and the one dependency that could pull it back.

The app is on mcp 2.x. It was held on 1.x for a while because the sandbox bridge, `opensandbox-mcp==0.1.1`, did
`from mcp.server.fastmcp import FastMCP`, and mcp 2.x removed that module (it is `mcp.server.mcpserver.MCPServer` there, and the
old path raises ModuleNotFoundError). `opensandbox-mcp` 1.1 moved to `MCPServer` and requires `mcp[cli]>=2,<3`, so one environment
serves both.

What would go wrong if that stopped being true is quiet: a pinned bridge that cannot import under the app's SDK is a bridge process
that dies at start, the loader degrades, and the sandbox is simply off (`agent_sandbox_unavailable_total{reason=other}`). These checks
fail that in CI, naming the cause.
"""
import re
from importlib.metadata import requires, version
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _pin(path: str, pattern: str) -> str:
    match = re.search(pattern, (REPO / path).read_text(), re.MULTILINE)
    assert match, f"no pin matching {pattern!r} found in {path}"
    return match.group(1)


def test_the_app_runs_on_the_2_x_sdk():
    assert version("mcp").split(".")[0] == "2", "the app's servers use mcp.server.MCPServer and its client the 2.x type names"


def test_requirements_and_the_lock_pin_the_same_2_x_release():
    declared = _pin("requirements.txt", r"^mcp\[cli\]==([0-9][^\s#]*)")
    locked = _pin("requirements-lock.txt", r"^mcp==([0-9][^\s#]*)")

    assert declared == locked
    assert declared.split(".")[0] == "2"


def test_the_sandbox_bridge_can_import_under_the_installed_sdk():
    """Fails with the reason, not as a dead sandbox in production, if the bridge on the path needs an SDK the app is not on."""
    try:
        import opensandbox_mcp.server  # noqa: F401 - the import itself is the check
    except ModuleNotFoundError as exc:
        raise AssertionError(
            f"opensandbox-mcp {version('opensandbox-mcp')} cannot import under mcp {version('mcp')}. Releases before 1.1 need "
            "`mcp.server.fastmcp`, which mcp 2.x removed; use opensandbox-mcp>=1.1 (requirements.txt)."
        ) from exc


def test_the_installed_bridge_declares_the_sdk_generation_the_app_is_on():
    """What the bridge itself says it needs, read from its metadata: a release that narrows this to 1.x would conflict with the pin."""
    needs = next(r for r in (requires("opensandbox-mcp") or []) if r.startswith("mcp"))

    assert ">=2" in needs.replace(" ", ""), f"opensandbox-mcp requires {needs!r}; the app is on mcp {version('mcp')}"
