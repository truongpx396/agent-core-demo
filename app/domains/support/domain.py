"""The support-copilot domain: a Tier-1 customer-facing agent, deployed
behind a chat gateway (see app/channels/telegram.py's AGENT_DOMAIN — the
same shape a WhatsApp Business Cloud API webhook would use, not built here,
see that module's docstring), with SANDBOXED tool access — literally just
the knowledge base plus the ticket system, nothing else.

`tools()` deliberately reuses `search_docs`/`skill_search`/`use_skill`/
`ask_clarification` from app/agent/tools.py AS-IS (same functions, same
TOOL_CAPABILITIES entries, same SecurityCtx/DEFAULT_POLICY enforcement
under the hood — see that module's docstring) rather than reimplementing
retrieval for a new domain. `SUPPORT_MANIFEST.allowed_tools` is exactly
those four plus this domain's own three ticket tools — deliberately
EXCLUDING calculator/add_note/remember/query_employees/run_subagent: a
Tier-1 support bot has no legitimate reason to do arbitrary arithmetic,
write to the shared knowledge base, remember cross-session facts, look up
employees, or delegate to a subagent. That omission — not a Policy check —
is literally what "sandboxed" means here (GRAPH_PATTERNS.md pattern 23:
`build_graph()`'s `ToolNode` only ever knows the tools this list names).
"""
from dataclasses import dataclass

from app.agent.manifest import AgentManifest, DomainPlugin
from app.agent.tools import TOOL_CAPABILITIES as _ACME_TOOL_CAPABILITIES
from app.agent.tools import ask_clarification, search_docs, skill_search, use_skill
from app.core.security import Policy
from app.domains.support.tools import SUPPORT_POLICY
from app.domains.support.tools import TOOL_CAPABILITIES as _SUPPORT_TOOL_CAPABILITIES
from app.domains.support.tools import TOOLS as _SUPPORT_TOOLS

_REUSED_READ_ONLY_TOOLS = [search_docs, skill_search, use_skill, ask_clarification]

SUPPORT_SYSTEM_PROMPT = """You are Acme Corp's Tier-1 customer support copilot.

Help customers using ONLY the knowledge base (search_docs) and the skill
catalog (skill_search/use_skill) — never guess at company policy or
procedure. If the knowledge base resolves the question, answer directly
with citations.

If it doesn't, or the request is something you cannot do yourself (a
refund, an account change, anything a customer explicitly asks a human
for), open a ticket with create_ticket and then escalate_to_human with a
clear reason. You have no tool that changes an account, issues a refund,
or does anything beyond search/ticket the knowledge base and ticket
system — that's intentional, not an oversight: escalate rather than
improvise.

Use check_ticket_status when a customer asks about an existing ticket, and
list_my_tickets when they ask about "my tickets" generally without a
number. If a customer follows up on a ticket they already opened with more
detail, use add_ticket_comment rather than opening a duplicate ticket for
the same issue.

Use ask_clarification when the request is genuinely ambiguous rather than
guessing what they meant."""


@dataclass
class _SupportDomainPlugin:
    def tools(self) -> list:
        return list(_SUPPORT_TOOLS) + list(_REUSED_READ_ONLY_TOOLS)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_SUPPORT_TOOL_CAPABILITIES)
        for name in ("search_docs", "skill_search", "use_skill", "ask_clarification"):
            merged[name] = _ACME_TOOL_CAPABILITIES[name]
        return merged

    def policy(self) -> Policy:
        return SUPPORT_POLICY


SUPPORT_DOMAIN_PLUGIN: DomainPlugin = _SupportDomainPlugin()

SUPPORT_MANIFEST = AgentManifest(
    name="support",
    system_prompt=SUPPORT_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in SUPPORT_DOMAIN_PLUGIN.tools()),
)
