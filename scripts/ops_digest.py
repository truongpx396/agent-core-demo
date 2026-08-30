"""Cron-callable ops digest: fetch this app's own operational metrics, flag
anomalies, summarize in plain language, post to the team channel.

Deliberately bypasses the ops domain's own agent loop (app/domains/ops/domain.py)
for the fetch+post — those are fixed, deterministic operations with no
legitimate need for an LLM to decide whether/how to call them, and running
them through build_graph() would hit should_continue's mandatory
human_approval gate (GRAPH_PATTERNS.md pattern 15): post_to_team_channel is
declared "outward" (app/domains/ops/tools.py), and that gate is
UNCONDITIONAL — there is no human present to approve an unattended cron
run. app/channels/telegram.py's own docstring discloses the identical
tension for its single-shot callers and resolves it by auto-declining;
auto-declining here would silently make "post the digest" never happen,
which defeats the whole job. So this script calls the ops domain's own
`_impl` functions directly (never through a ToolNode) and reserves the LLM
for the one thing it's actually needed for: turning raw numbers into
readable prose. scripts/ops_investigate.py, by contrast, is a human asking
an ad-hoc question — there the full agent loop is exactly the right tool
(and never hits the gate anyway, since fetch_metrics_summary is
read_only).

Meant to be wired to real cron/systemd-timer/a Kubernetes CronJob — e.g.:

    0 8 * * * cd /path/to/agent-core-demo && python -m scripts.ops_digest

Idempotent and side-effect-bounded to one team-channel post per run — safe
to re-run by hand (`make ops-digest` / `python -m scripts.ops_digest`) or
after a missed cron tick.
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
from app.domains.ops import metrics_client

# The automated job's own identity — never a real person's — so usage-ledger
# rows and team-channel posts from this script are attributable to "the
# cron job ran," distinct from any interactive user's own principal.
_CRON_CTX: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": "ops-cron", "claims": {}}

_DIGEST_SYSTEM_PROMPT = (
    "You write short, plain-language operational digests for an engineering "
    "team. Given a list of current metric readings and any flagged "
    "anomalies, write 3-6 sentences: what's normal, what (if anything) "
    "needs attention, and why. Be concrete — cite the actual numbers. "
    "Never invent a cause you can't support from the numbers given."
)


def build_digest_prompt(readings: dict[str, float | None], anomalies: list[str]) -> str:
    """Pure — the human-turn text handed to the summarization call. Kept
    separate from run_digest so it's directly unit-testable without an
    LLM."""
    lines = [
        "Today's metric readings:",
        metrics_client.format_readings(readings),
    ]
    if anomalies:
        lines.append("\nFlagged anomalies (past an alert-matching threshold):")
        lines.extend(f"- {a}" for a in anomalies)
    else:
        lines.append("\nNo anomalies — everything is within its normal range.")
    return "\n".join(lines)


def run_digest(llm=None) -> str:
    """Fetch → summarize → post. Returns the posted summary text. `llm` is
    DI for tests (mirrors app/agent/tools.py::_run_subagent_impl's own
    `llm` override) — defaults to a real ChatOpenAI client via the LiteLLM
    proxy, no tools bound (a plain completion, not a tool-calling turn —
    see this module's docstring for why)."""
    readings = metrics_client.fetch_readings()
    anomalies = metrics_client.detect_anomalies(readings)
    human_prompt = build_digest_prompt(readings, anomalies)

    chat = llm if llm is not None else ChatOpenAI(
        model=CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=SecretStr(OPENAI_API_KEY),
        temperature=0,
    )
    response = chat.invoke(
        [SystemMessage(content=_DIGEST_SYSTEM_PROMPT), HumanMessage(content=human_prompt)]
    )
    summary = response.content if isinstance(response.content, str) else str(response.content)

    usage = getattr(response, "usage_metadata", None) or {}
    total_tokens = usage.get("total_tokens", 0)
    if total_tokens:
        thread_id = f"ops-digest:{datetime.now(UTC).date().isoformat()}"
        record_usage(_CRON_CTX, thread_id, CHAT_MODEL, total_tokens)

    notify.post_to_team_channel("ops-digest", summary)
    return summary


if __name__ == "__main__":
    configure_logging()
    print(run_digest())
