"""The sales/CRM-concierge domain: watches inbound channels (via the
generalized app/channels/telegram.py, same AGENT_DOMAIN mechanism the
support copilot uses), logs interactions, drafts replies in the voice set
by SALES_SYSTEM_PROMPT below, schedules follow-ups a cron sweep
(scripts/followup_sweep.py) later nudges on, and hands a qualified lead to
a human rep with a packaged brief. Reuses search_docs/ask_clarification
from app/agent/tools.py as-is, same pattern app/domains/support/domain.py
already establishes.
"""
from dataclasses import dataclass

from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.tools import TOOL_CAPABILITIES as _ACME_TOOL_CAPABILITIES
from app.agent.tools import ask_clarification, search_docs
from app.core.security import Policy
from app.domains.sales.tools import SALES_POLICY
from app.domains.sales.tools import TOOL_CAPABILITIES as _SALES_TOOL_CAPABILITIES
from app.domains.sales.tools import TOOLS as _SALES_TOOLS

_REUSED_READ_ONLY_TOOLS = [search_docs, ask_clarification]

SALES_SYSTEM_PROMPT = """You are Acme Corp's sales concierge.

Voice: warm, concise, consultative — never pushy, never generic-sounding.
Draft replies as if writing them yourself, ready for a human rep to review
and send. You have NO tool that sends anything to a lead — every reply you
write is a draft for a human, always.

For every meaningful inbound message: call log_lead_interaction first. If
the lead needs a nudge later rather than an answer now, use
schedule_followup. Once a lead shows real buying intent (asks about
pricing, timeline, or explicitly wants to talk to someone), call
package_lead_brief and then handoff_to_human with a clear reason — don't
keep going back and forth with a lead that's ready for a person.

Use search_docs for product/company facts you're unsure of rather than
guessing, and ask_clarification when the lead's intent is genuinely
ambiguous."""


@dataclass
class _SalesDomainPlugin:
    def tools(self) -> list:
        return list(_SALES_TOOLS) + list(_REUSED_READ_ONLY_TOOLS)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_SALES_TOOL_CAPABILITIES)
        for name in ("search_docs", "ask_clarification"):
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
