"""Name → (AgentManifest, DomainPlugin) lookup for every domain this app
ships, consulted two different ways depending on whether "domain" is a
property of a whole PROCESS or of a single REQUEST:
- Per-process: app/channels/telegram.py and app/turns/agent_worker.py both
  read AGENT_DOMAIN once at startup and resolve it here — that process (or
  worker pool) serves exactly that one domain for its whole life. Running
  several domains at once this way means running several such processes,
  one per AGENT_DOMAIN.
- Per-request: app/api/main.py's queued endpoints (`POST
  /chat/stream/queued`, `/chat/resume`, `/chat/cancel`) validate the
  caller's `X-Domain` header against this registry's keys (see that
  module's `get_domain`) and route the request onto that domain's own
  Redis Stream (app/turns/queue.py::requests_stream_key) — which domain a
  MESSAGE is for, not the API process itself. This is what lets that ONE
  unified API process serve every domain a worker pool is currently
  running for.

Deliberately NOT consulted by app/channels/chat.py, which keeps defaulting
to Acme exactly as before this module existed — see app/agent/runtime.py's
init_graph_sync/init_graph_async docstrings for why that default needed no
code change here.
"""
from app.agent.manifest import (
    DEFAULT_DOMAIN_PLUGIN,
    DEFAULT_MANIFEST,
    AgentManifest,
    DomainPlugin,
)
from app.domains.ops.domain import OPS_DOMAIN_PLUGIN, OPS_MANIFEST
from app.domains.sales.domain import SALES_DOMAIN_PLUGIN, SALES_MANIFEST
from app.domains.support.domain import SUPPORT_DOMAIN_PLUGIN, SUPPORT_MANIFEST

DOMAINS: dict[str, tuple[AgentManifest, DomainPlugin]] = {
    "acme": (DEFAULT_MANIFEST, DEFAULT_DOMAIN_PLUGIN),
    "support": (SUPPORT_MANIFEST, SUPPORT_DOMAIN_PLUGIN),
    "ops": (OPS_MANIFEST, OPS_DOMAIN_PLUGIN),
    "sales": (SALES_MANIFEST, SALES_DOMAIN_PLUGIN),
}


def resolve_domain(name: str) -> tuple[AgentManifest, DomainPlugin]:
    """Fail loud on an unknown name — same discipline as
    TELEGRAM_BOT_TOKEN's missing-config check
    (app/channels/telegram.py::run) — rather than silently falling back to
    Acme, which would be a confusing way to discover a typo'd
    AGENT_DOMAIN."""
    try:
        return DOMAINS[name]
    except KeyError:
        raise ValueError(
            f"Unknown AGENT_DOMAIN {name!r} — must be one of: {', '.join(sorted(DOMAINS))}"
        ) from None
