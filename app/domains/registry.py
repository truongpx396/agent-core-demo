"""Name → (AgentManifest, DomainPlugin) lookup for every domain this app
ships, used by any process that boots the shared graph singleton against a
domain chosen at runtime rather than hardcoded — today just
app/channels/telegram.py (AGENT_DOMAIN). Deliberately NOT consulted by
app/api/main.py or app/channels/chat.py, which keep defaulting to Acme
exactly as before this module existed — see app/agent/runtime.py's
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
