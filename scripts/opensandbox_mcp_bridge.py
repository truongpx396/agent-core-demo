"""Thin wrapper around opensandbox-mcp's own CLI entrypoint that forces
`ConnectionConfig(use_server_proxy=True)` — GRAPH_PATTERNS.md pattern 50.

Necessary because `opensandbox-mcp==0.1.1`'s own CLI (`opensandbox_mcp.
__main__`) only ever forwards `api_key`/`domain`/`protocol`/
`request_timeout` into `ConnectionConfig` — there is no flag or env var to
set `use_server_proxy`, the exact setting the OpenSandbox SDK's own
`ConnectionConfig.use_server_proxy` field docstring names as "useful when
client sdk can't access the created sandbox directly": precisely this
process's own situation. It runs as a bare host process
(app/domains/sandbox_tools.py spawns it), while the containerized
`opensandbox-server` (docker/opensandbox-server.{Dockerfile,toml}, `make
sandbox-up`) and every sandbox container it creates live on a Docker
bridge network the host can't reach by container IP at all — a real,
verified PyPI version check confirms `0.1.1` is still latest, so there's
no newer release to upgrade to instead.

Verified directly, not assumed: without this, a real `Sandbox.create()`
call against the containerized server hung for 44+ seconds (this app's own
disclosed sandbox timeout, GRAPH_PATTERNS.md pattern 50) retrying GET
requests against a Docker-internal bridge IP
(`172.19.0.x:PORT/proxy/PORT/ping`) that's fundamentally unreachable from a
host process on Docker Desktop for Mac — not a speed problem, an
unreachable-address problem. With `use_server_proxy=True` (routes
health-check/exec traffic back through the server's own already-host-
reachable `:8090` instead of connecting to the sandbox directly), the
identical call succeeded in under 1.2s, real command execution included.

Argparse surface mirrors `opensandbox_mcp.__main__`'s own exactly (down to
the same conditional "only include a config key if the flag was actually
passed" behavior, since `ConnectionConfig`'s own fields fall back to
`OPEN_SANDBOX_API_KEY`/`OPEN_SANDBOX_DOMAIN` env vars when omitted — passing
an explicit `None` would shadow that fallback) so
app/domains/sandbox_tools.py's existing `--domain`/`--api-key`/`--protocol`
args keep working unchanged. Only stdio transport is kept (the only one
sandbox_tools.py ever uses).
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
