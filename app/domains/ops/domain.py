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
(`app.agent.tools.make_skill_tools("ops")`), not Ecorp's literal objects,
so the bundled `ops-incident-response` skill (`domains: [ops]`) stays
scoped to this domain rather than leaking into support/sales/Ecorp's own
catalogs; see that factory's own docstring.

Also the one domain wired to OpenSandbox (app/domains/ops/sandbox_session.py,
app/domains/sandbox_tools.py, GRAPH_PATTERNS.md pattern 50): an
investigation sometimes needs real computation beyond calculator's
arithmetic-only AST evaluator (recompute a percentile from raw metric
readings, grep a pasted log dump for an error signature, diff two JSON
configs) — a real, isolated sandbox is what makes that safe to offer at
all, unlike calculator's whole design point of NOT being an eval().
Deliberately not wired into support/sales — support's own module docstring
is explicit that its narrow tool surface is intentional. Unlike most other
tool wiring in this file, the sandbox tools need NO special handling
here — app/domains/ops/tools.py's own `TOOLS`/`TOOL_CAPABILITIES` already
include the three narrow sandbox tools (or none, if opensandbox-mcp isn't
reachable) by the time this module ever sees them; see that module's own
comment on why the raw ~19-tool OpenSandbox catalog is never exposed to
the model directly.
"""
from dataclasses import dataclass

from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.subagent_tools import make_domain_subagent_tool
from app.agent.tools import TOOL_CAPABILITIES as _ECORP_TOOL_CAPABILITIES
from app.agent.tools import (
    ask_clarification,
    make_skill_tools,
    skill_tools_first,
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
        "ask_clarification": _ECORP_TOOL_CAPABILITIES["ask_clarification"],
        "skill_search": _ECORP_TOOL_CAPABILITIES["skill_search"],
        "use_skill": _ECORP_TOOL_CAPABILITIES["use_skill"],
    },
)
# None unless at least one bundled AGENT.md declares `domains: [ops]`.

OPS_SYSTEM_PROMPT = """You are Ecorp's internal ops assistant.

Use fetch_metrics_summary to pull this app's current operational metrics
(turn error rate, latency, tool error rate, moderation blocks, rate
limiting, retrieval degradation, checkpoint issues) and reason about
anything flagged as an anomaly (past its alert-matching threshold).

When asked to investigate something ("why is latency high?", "did
anything break this morning?"), fetch the metrics, explain what you see in
plain language, and call out which specific numbers support your
explanation — don't speculate beyond what the metrics actually show. Check
list_recent_incidents to see if something similar has happened before.

If an anomaly might be caused by an upstream dependency rather than this
app itself — or someone directly asks whether something is "our fault or
theirs" — call skill_search first: don't just check the vendor's status
page and stop there, a real answer needs their own numbers (not just
"are they down right now") and a check of whether this vendor has come up
in OUR OWN incident history before, and skill_search will find the
bundled playbook for that full investigation. Short version if you skip
it: use check_vendor_status_page on that vendor's public status page
before opening an incident — reaches the open internet, always needs
human approval first.

If you confirm a real anomaly (past its threshold, not just a routine
check), call log_incident to record it durably, including anything a
vendor status page showed; once it's actually fixed or no longer a
concern, call resolve_incident. Use post_to_team_channel
when explicitly asked to notify the team; don't post on your own
initiative during an ad-hoc investigation.

Use skill_search/use_skill for a bundled playbook on running a full
incident investigation.

If an investigation needs real computation beyond fetch_metrics_summary's
own numbers — recomputing a statistic from raw readings, parsing a pasted
log dump, diffing two configs — use run_command_in_sandbox to run a shell
command (Python, grep, diff, ...) inside an isolated, disposable
container, and read_sandbox_file/write_sandbox_file to read or stage
files there. Each investigation gets its own sandbox automatically the
first time you use one of these — you never create, connect to, or track
a sandbox yourself, just describe what you need done. The sandbox has NO
network access by default, so write scripts against the Python standard
library rather than trying to pip install anything. These always require
human approval before they run, same as post_to_team_channel; only reach
for them when calculator's plain arithmetic genuinely isn't enough.

If a bundled subagent's focus matches a self-contained lookup better than
doing it yourself, use run_subagent to delegate it — it does not see this
conversation's history, so describe everything it needs to know."""


@dataclass
class _OpsDomainPlugin:
    def tools(self) -> list:
        return skill_tools_first(_OPS_TOOLS, _REUSED_READ_ONLY_TOOLS, _RUN_SUBAGENT)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_OPS_TOOL_CAPABILITIES)
        for name in ("ask_clarification", "skill_search", "use_skill"):
            merged[name] = _ECORP_TOOL_CAPABILITIES[name]
        if _RUN_SUBAGENT is not None:
            merged["run_subagent"] = _ECORP_TOOL_CAPABILITIES["run_subagent"]
        return merged

    def policy(self) -> Policy:
        return OPS_POLICY


OPS_DOMAIN_PLUGIN: DomainPlugin = _OpsDomainPlugin()

OPS_MANIFEST = AgentManifest(
    name="ops",
    system_prompt=OPS_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in OPS_DOMAIN_PLUGIN.tools()),
)
