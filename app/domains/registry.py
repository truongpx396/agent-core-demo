"""Name -> (AgentManifest, DomainPlugin) lookup, consulted two ways:
- Per-process: telegram.py/agent_worker.py read AGENT_DOMAIN once at
  startup and resolve it here — that process/worker pool serves exactly
  one domain for its life.
- Per-request: app/api/main.py's queued endpoints validate the caller's
  `X-Domain` header against this registry's keys and route onto that
  domain's own Redis Stream — which domain a MESSAGE is for, letting one
  API process serve every domain a worker pool is running.

Deliberately NOT consulted by app/channels/chat.py, which keeps defaulting
to Ecorp unchanged (see runtime.py's init_graph_async docstring).
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
    "ecorp": (DEFAULT_MANIFEST, DEFAULT_DOMAIN_PLUGIN),
    "support": (SUPPORT_MANIFEST, SUPPORT_DOMAIN_PLUGIN),
    "ops": (OPS_MANIFEST, OPS_DOMAIN_PLUGIN),
    "sales": (SALES_MANIFEST, SALES_DOMAIN_PLUGIN),
}


def resolve_domain(name: str) -> tuple[AgentManifest, DomainPlugin]:
    """Fail loud on an unknown name rather than silently falling back to
    Ecorp, which would be a confusing way to discover a typo'd
    AGENT_DOMAIN."""
    try:
        return DOMAINS[name]
    except KeyError:
        raise ValueError(
            f"Unknown AGENT_DOMAIN {name!r} — must be one of: {', '.join(sorted(DOMAINS))}"
        ) from None
