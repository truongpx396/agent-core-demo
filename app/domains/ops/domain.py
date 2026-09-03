"""The internal ops-bot domain: fetch this app's own operational metrics,
flag anomalies, post a digest to the team channel, and log/list/resolve
durable incidents (app/domains/ops/store.py) — plus, via
scripts/ops_investigate.py, answer an ad-hoc "why is X happening" question
using the same tools directly. See scripts/ops_digest.py's own docstring
for why the CRON digest itself bypasses this domain's agent loop entirely
(a fixed, deterministic pipeline, not a tool-calling turn — an unattended
cron job can never satisfy should_continue's mandatory human_approval gate
that post_to_team_channel's "outward" capability requires). Reuses
skill_search/use_skill from app/agent/tools.py as-is, same pattern
app/domains/support/domain.py establishes, including the bundled
`ops-incident-response` skill under skills/.
"""
from dataclasses import dataclass

from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.tools import TOOL_CAPABILITIES as _ACME_TOOL_CAPABILITIES
from app.agent.tools import ask_clarification, skill_search, use_skill
from app.core.security import Policy
from app.domains.ops.tools import OPS_POLICY
from app.domains.ops.tools import TOOL_CAPABILITIES as _OPS_TOOL_CAPABILITIES
from app.domains.ops.tools import TOOLS as _OPS_TOOLS

_REUSED_READ_ONLY_TOOLS = [ask_clarification, skill_search, use_skill]

OPS_SYSTEM_PROMPT = """You are Acme Corp's internal ops assistant.

Use fetch_metrics_summary to pull this app's current operational metrics
(turn error rate, latency, tool error rate, moderation blocks, rate
limiting, retrieval degradation, checkpoint issues) and reason about
anything flagged as an anomaly (past its alert-matching threshold).

When asked to investigate something ("why is latency high?", "did
anything break this morning?"), fetch the metrics, explain what you see in
plain language, and call out which specific numbers support your
explanation — don't speculate beyond what the metrics actually show. Check
list_recent_incidents to see if something similar has happened before.

If you confirm a real anomaly (past its threshold, not just a routine
check), call log_incident to record it durably; once it's actually fixed
or no longer a concern, call resolve_incident. Use post_to_team_channel
when explicitly asked to notify the team; don't post on your own
initiative during an ad-hoc investigation.

Use skill_search/use_skill for a bundled playbook on running a full
incident investigation."""


@dataclass
class _OpsDomainPlugin:
    def tools(self) -> list:
        return list(_OPS_TOOLS) + list(_REUSED_READ_ONLY_TOOLS)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_OPS_TOOL_CAPABILITIES)
        for name in ("ask_clarification", "skill_search", "use_skill"):
            merged[name] = _ACME_TOOL_CAPABILITIES[name]
        return merged

    def policy(self) -> Policy:
        return OPS_POLICY


OPS_DOMAIN_PLUGIN: DomainPlugin = _OpsDomainPlugin()

OPS_MANIFEST = AgentManifest(
    name="ops",
    system_prompt=OPS_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in OPS_DOMAIN_PLUGIN.tools()),
)
