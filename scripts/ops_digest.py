"""Cron-callable ops digest: fetch this app's operational metrics, flag anomalies, summarize in plain language
and post to the team channel. It bypasses the ops domain's agent loop (app/domains/ops/domain.py): fetch and
post are fixed, deterministic operations, and through build_graph() they would hit should_continue's mandatory
human_approval gate (GRAPH_PATTERNS.md pattern 15), which an unattended cron run can't satisfy, and
auto-declining (as app/channels/telegram.py does) would silently make "post the digest" never happen. So it
calls the ops `_impl` functions directly, reserving the LLM for turning numbers into prose. Contrast
scripts/ops_investigate.py, where a human asks an ad-hoc question and the agent loop is right.

Wire it to cron, a systemd timer or a Kubernetes CronJob, e.g. `0 8 * * * cd /path/to/agent-core-demo &&
python -m scripts.ops_digest`. One team-channel post per run, safe to re-run by hand or after a missed tick.
"""
import asyncio
from datetime import UTC, datetime

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.agent.cost import metering
from app.core.config import CHAT_MODEL, DEFAULT_TENANT, OPENAI_API_BASE, OPENAI_API_KEY
from app.core.job_runtime import scheduled_job
from app.core.security import SecurityCtx
from app.domains.ops import metrics_client
from app.ports.notify import team_channel

# The automated job's own identity — never a real person's — so usage-event
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


async def run_digest(llm=None) -> str:
    """Fetch → summarize → post. Returns the posted summary text. `llm` is
    DI for tests (mirrors app/agent/tools/run_subagent.py::run_subagent_impl's own
    `llm` override) — defaults to a real ChatOpenAI client via the LiteLLM
    proxy, no tools bound (a plain completion, not a tool-calling turn —
    see this module's docstring for why)."""
    readings = await metrics_client.fetch_readings()
    anomalies = metrics_client.detect_anomalies(readings)
    human_prompt = build_digest_prompt(readings, anomalies)

    chat = llm if llm is not None else ChatOpenAI(
        model=CHAT_MODEL,
        base_url=OPENAI_API_BASE,
        api_key=SecretStr(OPENAI_API_KEY),
        temperature=0,
    )
    # One metered call (app/agent/cost/metering.py): identity for the gateway, priced once, one usage
    # event (a cron job has no turn whose total could carry it, and the event is what the caps count).
    thread_id = f"ops-digest:{datetime.now(UTC).date().isoformat()}"
    call = await metering.metered_invoke(
        chat,
        [SystemMessage(content=_DIGEST_SYSTEM_PROMPT), HumanMessage(content=human_prompt)],
        config={"configurable": {"ctx": _CRON_CTX, "thread_id": thread_id}},
        kind="cron",
        model_alias=CHAT_MODEL,
    )
    response = call.response
    summary = response.content if isinstance(response.content, str) else str(response.content)

    await team_channel.get().post("ops-digest", summary)
    return summary


def main() -> None:
    with scheduled_job("agent-core-ops-digest"):
        print(asyncio.run(run_digest()))


if __name__ == "__main__":
    main()
