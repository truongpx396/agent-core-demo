"""scripts/opensandbox_mcp_bridge.py: the thin wrapper that starts `opensandbox-mcp` with `use_server_proxy=True`.

The one behaviour that matters beyond that flag is where the API key comes from, because the app no longer puts it on the
bridge's command line (app/domains/sandbox_tools.py, specs/009 A5): with no `--api-key` the SDK must resolve it from
OPEN_SANDBOX_API_KEY. That is a promise made by a third-party library, so it is checked here against the installed one
instead of assumed; if a release stops honouring the variable, the sandbox would silently authenticate with nothing.
"""
import sys

from scripts import opensandbox_mcp_bridge as bridge


def _run_bridge(monkeypatch, *argv):
    captured = {}

    class _Server:
        def run(self, transport):
            captured["transport"] = transport

    def fake_create_server(connection_config):
        captured["config"] = connection_config
        return _Server()

    monkeypatch.setattr(bridge, "create_server", fake_create_server)
    monkeypatch.setattr(sys, "argv", ["opensandbox_mcp_bridge.py", *argv])
    bridge.main()
    return captured


def test_without_an_api_key_flag_the_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPEN_SANDBOX_API_KEY", "from-the-environment")

    captured = _run_bridge(monkeypatch, "--domain", "localhost:8090", "--protocol", "http")

    assert captured["config"].get_api_key() == "from-the-environment"
    assert captured["transport"] == "stdio"


def test_the_bridge_always_routes_through_the_server_proxy(monkeypatch):
    captured = _run_bridge(monkeypatch, "--domain", "localhost:8090")

    assert captured["config"].use_server_proxy is True
