"""The internal ops-bot domain: fetch this app's own operational metrics,
flag anomalies, post a digest to the team channel, and log/list/resolve
durable incidents (app/domains/ops/store.py) — plus, via
scripts/ops_investigate.py, answer an ad-hoc "why is X happening" question
using the same tools directly. See scripts/ops_digest.py's own docstring
for why the CRON digest itself bypasses this domain's agent loop entirely
(a fixed, deterministic pipeline, not a tool-calling turn — an unattended
cron job can never satisfy should_continue's mandatory human_approval gate
that post_to_team_channel's "outward" capability requires). skill_search/
use_skill are this domain's OWN pair
(`app.agent.tools.make_skill_tools("ops")`), not Acme's literal objects,
so the bundled `ops-incident-response` skill (`domains: [ops]`) stays
scoped to this domain rather than leaking into support/sales/Acme's own
catalogs; see that factory's own docstring.
"""
from dataclasses import dataclass

from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.tools import TOOL_CAPABILITIES as _ACME_TOOL_CAPABILITIES
from app.agent.tools import (
    ask_clarification,
    make_domain_subagent_tool,
    make_skill_tools,
)
from app.core.security import Policy
from app.domains.ops.tools import OPS_POLICY
from app.domains.ops.tools import TOOL_CAPABILITIES as _OPS_TOOL_CAPABILITIES
from app.domains.ops.tools import TOOLS as _OPS_TOOLS

_SKILL_SEARCH, _USE_SKILL = make_skill_tools("ops")

_REUSED_READ_ONLY_TOOLS = [ask_clarification, _SKILL_SEARCH, _USE_SKILL]

_RUN_SUBAGENT = make_domain_subagent_tool(
    domain="ops",
    all_tools=list(_OPS_TOOLS) + _REUSED_READ_ONLY_TOOLS,
    tool_capabilities={
        **_OPS_TOOL_CAPABILITIES,
        "ask_clarification": _ACME_TOOL_CAPABILITIES["ask_clarification"],
        "skill_search": _ACME_TOOL_CAPABILITIES["skill_search"],
        "use_skill": _ACME_TOOL_CAPABILITIES["use_skill"],
    },
)
# None unless at least one bundled AGENT.md declares `domains: [ops]`.

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
incident investigation.

If a bundled subagent's focus matches a self-contained lookup better than
doing it yourself, use run_subagent to delegate it — it does not see this
conversation's history, so describe everything it needs to know."""


@dataclass
class _OpsDomainPlugin:
    def tools(self) -> list:
        tools = list(_OPS_TOOLS) + list(_REUSED_READ_ONLY_TOOLS)
        if _RUN_SUBAGENT is not None:
            tools.append(_RUN_SUBAGENT)
        return tools

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_OPS_TOOL_CAPABILITIES)
        for name in ("ask_clarification", "skill_search", "use_skill"):
            merged[name] = _ACME_TOOL_CAPABILITIES[name]
        if _RUN_SUBAGENT is not None:
            merged["run_subagent"] = _ACME_TOOL_CAPABILITIES["run_subagent"]
        return merged

    def policy(self) -> Policy:
        return OPS_POLICY


OPS_DOMAIN_PLUGIN: DomainPlugin = _OpsDomainPlugin()

OPS_MANIFEST = AgentManifest(
    name="ops",
    system_prompt=OPS_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in OPS_DOMAIN_PLUGIN.tools()),
)
