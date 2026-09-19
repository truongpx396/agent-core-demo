"""Thin wrapper around opensandbox-mcp's own CLI entrypoint that forces
`ConnectionConfig(use_server_proxy=True)` — GRAPH_PATTERNS.md pattern 50.

Necessary because `opensandbox-mcp==0.1.1`'s CLI only forwards
`api_key`/`domain`/`protocol`/`request_timeout` into `ConnectionConfig` —
no flag/env var sets `use_server_proxy`, needed because this process runs
as a bare host process (app/domains/sandbox_tools.py spawns it) while the
containerized `opensandbox-server` and every sandbox container it creates
live on a Docker bridge network the host can't reach by container IP at
all (0.1.1 is still latest on PyPI — no newer release fixes this).

Verified directly: without this, `Sandbox.create()` against the
containerized server hung 44+ seconds (this app's disclosed sandbox
timeout, pattern 50) retrying an unreachable Docker-internal bridge IP.
With `use_server_proxy=True` (routes health-check/exec traffic through the
server's host-reachable `:8090` instead of the sandbox directly), the same
call succeeded in under 1.2s.

Argparse surface mirrors `opensandbox_mcp.__main__` exactly, including
only including a config key if its flag was passed (`ConnectionConfig`
otherwise falls back to `OPEN_SANDBOX_API_KEY`/`OPEN_SANDBOX_DOMAIN` env
vars) — so app/domains/sandbox_tools.py's existing args keep working
unchanged. Only stdio transport is kept (the only one it uses).
"""
import argparse
from datetime import timedelta
from typing import Any

from opensandbox.config import ConnectionConfig
from opensandbox_mcp.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpenSandbox MCP bridge, always with use_server_proxy=True."
    )
    parser.add_argument("--api-key", default=None, help="OpenSandbox API key (overrides OPEN_SANDBOX_API_KEY).")
    parser.add_argument("--domain", default=None, help="OpenSandbox API domain (overrides OPEN_SANDBOX_DOMAIN).")
    parser.add_argument("--protocol", choices=("http", "https"), default="http")
    parser.add_argument("--request-timeout-seconds", type=float, default=30)
    args = parser.parse_args()

    config_values: dict[str, Any] = {"use_server_proxy": True}
    if args.api_key:
        config_values["api_key"] = args.api_key
    if args.domain:
        config_values["domain"] = args.domain
    if args.protocol:
        config_values["protocol"] = args.protocol
    if args.request_timeout_seconds is not None:
        config_values["request_timeout"] = timedelta(seconds=args.request_timeout_seconds)

    mcp = create_server(connection_config=ConnectionConfig(**config_values))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
