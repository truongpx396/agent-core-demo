"""Ad-hoc ops investigation: ask the ops domain's agent a one-off question ("why is latency high right now?")
with its full toolset (app/domains/ops/tools.py). Most investigations are read-only and never pause, but a
mutating call (log_incident, resolve_incident, post_to_team_channel) WILL hit the mandatory human_approval
gate, and this one-shot invocation has no resume loop: `investigate()` returns the last AIMessage, typically
empty. Disclosed: an empty answer on a mutating call is the signal a human needs to be in the loop.

A one-shot `build_graph(manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)`, not `run_subagent`: the ops
`run_subagent` delegates to `metrics-researcher`, restricted to
`fetch_metrics_summary`/`list_recent_incidents`, narrower than open-ended questions need. No durable
checkpointer (the default in-memory MemorySaver): one question, one answer, no memory across runs.

Run with `python -m scripts.ops_investigate "why is latency high right now?"`.
"""
import asyncio
import getpass
import sys
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.graph.build import build_graph
from app.core.config import DEFAULT_TENANT
from app.core.job_runtime import scheduled_job
from app.core.security import SecurityCtx
from app.domains.ops.domain import OPS_DOMAIN_PLUGIN, OPS_MANIFEST

# Local dev ctx, same shape as app/channels/chat.py's _LOCAL_CTX — this
# process itself is the trusted boundary a real deployment's auth gateway
# would otherwise stamp.
_LOCAL_CTX: SecurityCtx = {
    "tenant": DEFAULT_TENANT,
    "principal": f"local:{getpass.getuser()}",
    "claims": {},
}


async def investigate(question: str) -> str:
    graph = build_graph(manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)
    config = {
        "configurable": {
            "thread_id": f"ops-investigate:{uuid.uuid4().hex[:8]}",
            "ctx": _LOCAL_CTX,
        }
    }
    # `ainvoke`, not `.invoke()` — the graph's nodes are `async def` now
    # (app/agent/graph/core.py); LangGraph's sync Pregel loop can't run one.
    result = await graph.ainvoke(
        {
            "messages": [
                SystemMessage(content=OPS_MANIFEST.system_prompt),
                HumanMessage(content=question),
            ],
            "require_approval": False,
        },
        config=config,
    )
    final_ai = next(
        (m for m in reversed(result["messages"]) if isinstance(m, AIMessage)), None
    )
    if final_ai is None:
        return "(no answer produced)"
    return final_ai.content if isinstance(final_ai.content, str) else str(final_ai.content)


def main() -> None:
    with scheduled_job("agent-core-ops-investigate"):
        question = " ".join(sys.argv[1:]) or "Is anything unusual right now?"
        print(asyncio.run(investigate(question)))


if __name__ == "__main__":
    main()
