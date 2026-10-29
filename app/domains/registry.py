"""Name -> (AgentManifest, DomainPlugin) lookup, consulted two ways:
- Per-process: agent_worker.py, telegram.py and the CLI (chat.py) read
  AGENT_DOMAIN once at startup and resolve it here. The worker builds its
  graph from the result and serves exactly one domain for its life; the two
  channels only validate the name (a typo fails at startup rather than as a
  wait for a worker pool that does not exist) and publish their turns to that
  domain's stream. Neither channel builds a graph.
- Per-request: app/api/deps.py's `get_domain` validates the caller's
  `X-Domain` header against this registry's keys, and the queued chat
  endpoints (app/api/routers/chat.py) route onto that
  domain's own Redis Stream — which domain a MESSAGE is for, letting one
  API process serve every domain a worker pool is running.
"""
from app.domains.ecorp.domain import ECORP_DOMAIN_PLUGIN, ECORP_MANIFEST
from app.domains.ops.domain import OPS_DOMAIN_PLUGIN, OPS_MANIFEST
from app.domains.sales.domain import SALES_DOMAIN_PLUGIN, SALES_MANIFEST
from app.domains.support.domain import SUPPORT_DOMAIN_PLUGIN, SUPPORT_MANIFEST
from app.ports.domains import AgentManifest, DomainPlugin

DOMAINS: dict[str, tuple[AgentManifest, DomainPlugin]] = {
    "ecorp": (ECORP_MANIFEST, ECORP_DOMAIN_PLUGIN),
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


class RegistryDomainCatalog:
    """The `DomainCatalog` adapter (`app/ports/domains.py`): the table above, looked up by the module-level functions so a test that patches
    `DOMAINS` is seen."""

    def resolve(self, name: str) -> tuple[AgentManifest, DomainPlugin]:
        return resolve_domain(name)

    def names(self) -> list[str]:
        return sorted(DOMAINS)
