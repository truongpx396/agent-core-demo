"""Cron-callable follow-up sweep: find every CRM follow-up due today
(app/domains/sales/store.py::due_followups), draft a nudge in the sales
concierge's own voice for each, and post the draft to the team channel for
a human to review and actually send.

Same "fixed pipeline, not an agent turn" shape as scripts/ops_digest.py,
for the same reason: an unattended cron run can never satisfy
should_continue's mandatory human_approval gate, so this never calls
schedule_followup/handoff_to_human/log_lead_interaction through the real
agent loop — those are for the INTERACTIVE sales domain (via the
generalized app/channels/telegram.py, a human actually present in the
conversation) to call. This script never auto-sends anything and never
auto-hands off a lead on its own — it only drafts, mirroring the use
case's own "drafts replies in your voice" and "hands hot leads to humans"
language: the human stays in the loop for both the send and the handoff
decision.

Meant to be wired to real cron, e.g.:

    0 9 * * * cd /path/to/agent-core-demo && python -m scripts.followup_sweep

Each due follow-up is marked 'done' once swept (postgres-init/08-crm.sql's
crm_followups.status) so a re-run of this same script doesn't redraft the
same nudge tomorrow — a missed follow-up doesn't get lost, it's just
handled once, at whichever run first sees it past its due_at.
"""
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.agent.meter import record_usage
from app.core.config import CHAT_MODEL, DEFAULT_TENANT, OPENAI_API_BASE, OPENAI_API_KEY
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.domains import notify
from app.domains.sales import store
from app.domains.sales.domain import SALES_MANIFEST

_CRON_CTX: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": "sales-followup-cron", "claims": {}}

_DRAFT_TASK_INSTRUCTIONS = """

You are now drafting ONE follow-up nudge for a lead who is due for contact
today. You'll be given their name, the note left when this follow-up was
scheduled, and their interaction history so far. Write a short,
ready-to-send message in the voice described above — a human will review
it before anything goes out, so write the message itself, not a
description of what you'd say."""


def build_followup_prompt(lead_name: str, contact: str, note: str) -> str:
    """Pure — the human-turn text for one lead's drafting call. Kept
    separate from run_followup_sweep so it's directly unit-testable
    without an LLM."""
    return (
        f"Lead: {lead_name} ({contact})\n"
        f"Follow-up note: {note}\n\n"
        "Draft the nudge message now."
    )


def run_followup_sweep(tenant: str = DEFAULT_TENANT, llm=None) -> list[str]:
    """Sweep every due follow-up for `tenant`, draft a nudge for each, post
    it to the team channel, mark it done. Returns the list of drafted
    texts (empty if nothing was due). `llm` is DI for tests, same pattern
    as scripts/ops_digest.py::run_digest."""
    due = store.due_followups(tenant, datetime.now(UTC))
    if not due:
        return []

    chat = llm if llm is not None else ChatOpenAI(
        model=CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=SecretStr(OPENAI_API_KEY),
        temperature=0,
    )
    system_prompt = SALES_MANIFEST.system_prompt + _DRAFT_TASK_INSTRUCTIONS

    drafts = []
    for item in due:
        human_prompt = build_followup_prompt(item["lead_name"], item["contact"], item["note"])
        response = chat.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
        )
        draft = response.content if isinstance(response.content, str) else str(response.content)

        usage = getattr(response, "usage_metadata", None) or {}
        total_tokens = usage.get("total_tokens", 0)
        if total_tokens:
            record_usage(_CRON_CTX, f"followup-sweep:{item['id']}", CHAT_MODEL, total_tokens)

        notify.post_to_team_channel(
            "sales-followups",
            f"Draft nudge for {item['lead_name']} ({item['contact']}):\n{draft}",
        )
        store.mark_followup_done(tenant, item["id"])
        drafts.append(draft)

    return drafts


if __name__ == "__main__":
    configure_logging()
    drafts = run_followup_sweep()
    print(f"Drafted {len(drafts)} follow-up nudge(s)." if drafts else "No follow-ups due.")
