"""The sandbox bridge's tool catalog against the hashes this app pinned (app/domains/sandbox_tools.py::PINNED_SANDBOX_TOOLS).

The client keeps a remote tool only while its name, description and input schema hash to the pinned value (app/mcp/client.py,
specs/009 A5). That is the right behaviour in production and a trap in a dependency bump: a release of `opensandbox-mcp` that
rewrites a description would switch the sandbox off at runtime. This test is where the bump is caught instead, in CI, with the
new values printed next to the ones to replace.

It imports the bridge's server in-process (no subprocess, no backend: the catalog is static) and checks first that the in-process
listing is what the wire would carry, by hashing through the same function the client uses.
"""
from opensandbox.config import ConnectionConfig
from opensandbox_mcp.server import create_server

from app.domains import sandbox_session, sandbox_tools
from app.mcp.client import tool_fingerprint


async def _offered() -> dict[str, str]:
    server = create_server(connection_config=ConnectionConfig(use_server_proxy=True))
    return {t.name: tool_fingerprint(t.name, t.description, t.input_schema) for t in await server.list_tools()}


async def test_every_pinned_sandbox_tool_still_matches_what_the_installed_bridge_offers():
    offered = await _offered()

    drifted = {name: offered.get(name) for name, pin in sandbox_tools.PINNED_SANDBOX_TOOLS.items() if offered.get(name) != pin}

    assert not drifted, (
        "The installed opensandbox-mcp describes these tools differently from what app/domains/sandbox_tools.py pinned, so the "
        "app would leave them out at runtime (agent_mcp_tool_pin_failures_total). Read what changed in their descriptions and "
        "schemas, then update PINNED_SANDBOX_TOOLS to:\n"
        + "\n".join(f'    "{name}": "{offered[name]}",' for name in sorted(drifted) if offered.get(name))
        + ("\n(missing from the bridge now: " + ", ".join(n for n in sorted(drifted) if not offered.get(n)) + ")" if any(not offered.get(n) for n in drifted) else "")
    )


def test_the_pins_are_exactly_the_tools_sandbox_session_calls():
    """Pinning more than is used keeps unused (and unreviewed) tools in the model's hands; pinning fewer switches a used one off."""
    import inspect
    import re

    source = inspect.getsource(sandbox_session)
    called = set(re.findall(r'_call_raw_tool\(\s*raw,\s*"([a-z_]+)"', source))

    assert called == set(sandbox_tools.PINNED_SANDBOX_TOOLS)
