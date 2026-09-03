"""The sales/CRM-concierge domain: watches inbound channels (via the
generalized app/channels/telegram.py, same AGENT_DOMAIN mechanism the
support copilot uses), logs interactions, drafts replies in the voice set
by SALES_SYSTEM_PROMPT below, schedules follow-ups a cron sweep
(scripts/followup_sweep.py) later nudges on, checks the pending follow-up
queue, hands a qualified lead to a human rep with a packaged brief, and
closes out a lead that isn't converting. Reuses search_docs/skill_search/
use_skill/ask_clarification from app/agent/tools.py as-is, same pattern
app/domains/support/domain.py already establishes (including the bundled
`sales-lead-qualification` skill under skills/ this domain's skill_search
can now find).
"""
from dataclasses import dataclass

from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.tools import TOOL_CAPABILITIES as _ACME_TOOL_CAPABILITIES
from app.agent.tools import ask_clarification, search_docs, skill_search, use_skill
from app.core.security import Policy
from app.domains.sales.tools import SALES_POLICY
from app.domains.sales.tools import TOOL_CAPABILITIES as _SALES_TOOL_CAPABILITIES
from app.domains.sales.tools import TOOLS as _SALES_TOOLS

_REUSED_READ_ONLY_TOOLS = [search_docs, skill_search, use_skill, ask_clarification]

SALES_SYSTEM_PROMPT = """You are Acme Corp's sales concierge.

Voice: warm, concise, consultative — never pushy, never generic-sounding.
Draft replies as if writing them yourself, ready for a human rep to review
and send. You have NO tool that sends anything to a lead — every reply you
write is a draft for a human, always.

For every meaningful inbound message: call log_lead_interaction first. If
the lead needs a nudge later rather than an answer now, use
schedule_followup — check list_pending_followups first so you don't
schedule a second one on top of an existing pending one. Once a lead shows
real buying intent (asks about pricing, timeline, or explicitly wants to
talk to someone), call package_lead_brief and then handoff_to_human with a
clear reason — don't keep going back and forth with a lead that's ready
for a person. If a lead clearly isn't going to convert (explicitly not
interested, or unresponsive after repeated follow-ups), call
mark_lead_lost with a specific reason rather than leaving it to keep
surfacing in the follow-up queue.

Use search_docs for product/company facts you're unsure of rather than
guessing, skill_search/use_skill for a bundled playbook on handling a
lead's full lifecycle, and ask_clarification when the lead's intent is
genuinely ambiguous."""


@dataclass
class _SalesDomainPlugin:
    def tools(self) -> list:
        return list(_SALES_TOOLS) + list(_REUSED_READ_ONLY_TOOLS)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_SALES_TOOL_CAPABILITIES)
        for name in ("search_docs", "skill_search", "use_skill", "ask_clarification"):
            merged[name] = _ACME_TOOL_CAPABILITIES[name]
        return merged

    def policy(self) -> Policy:
        return SALES_POLICY


SALES_DOMAIN_PLUGIN: DomainPlugin = _SalesDomainPlugin()

SALES_MANIFEST = AgentManifest(
    name="sales",
    system_prompt=SALES_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in SALES_DOMAIN_PLUGIN.tools()),
)
