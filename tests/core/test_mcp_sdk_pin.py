"""Why `mcp` is held on the 1.x line, and the check that says so when someone moves it.

The reason is not this app's own code, which needs only a rename to run on mcp 2.x: the sandbox bridge, `opensandbox-mcp`, does
`from mcp.server.fastmcp import FastMCP`, and mcp 2.x removed that module (it is `mcp.server.mcpserver.MCPServer` there, and the old
path raises ModuleNotFoundError). A bump past 1.x would therefore not fail anything this app imports; it would silently switch the
sandbox off (the bridge process dies at start, the loader degrades, `agent_sandbox_unavailable_total{reason=other}` rises). This
fails the bump in CI instead, naming the cause.
"""
import re
from importlib.metadata import version
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _pin(path: str, pattern: str) -> str:
    match = re.search(pattern, (REPO / path).read_text(), re.MULTILINE)
    assert match, f"no mcp pin found in {path}"
    return match.group(1)


def test_the_sandbox_bridge_can_still_import_the_sdk_module_it_needs():
    """Fails under mcp 2.x with the reason, not as a dead sandbox in production."""
    try:
        import opensandbox_mcp.server  # noqa: F401 - the import itself is the check
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "opensandbox-mcp cannot import under the installed mcp SDK. It needs `mcp.server.fastmcp`, which mcp 2.x removed. "
            "Keep mcp on 1.x until opensandbox-mcp supports 2.x (requirements.txt explains), or replace the bridge."
        ) from exc


def test_the_installed_sdk_is_on_the_1_x_line():
    assert version("mcp").split(".")[0] == "1", "mcp 2.x removed mcp.server.fastmcp, which opensandbox-mcp imports; see requirements.txt"


def test_requirements_and_the_lock_pin_the_same_mcp_release_on_the_1_x_line():
    declared = _pin("requirements.txt", r"^mcp\[cli\]==([0-9][^\s#]*)")
    locked = _pin("requirements-lock.txt", r"^mcp==([0-9][^\s#]*)")

    assert declared == locked
    assert declared.split(".")[0] == "1"
