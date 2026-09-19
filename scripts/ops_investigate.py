"""Ad-hoc ops investigation: ask the ops domain's own agent a one-off
question ("why is latency high right now?") using its full toolset
(app/domains/ops/tools.py). Most investigations stay read-only and never
pause — but a mutating tool call (log_incident, resolve_incident,
post_to_team_channel) WILL hit should_continue's mandatory human_approval
gate like any caller, and this one-shot invocation has no resume loop:
`investigate()` just returns whatever's in the last AIMessage (typically
empty). Disclosed, not papered over — an empty answer on a mutating call
is the honest signal a human needs to be in the loop instead.

A one-shot `build_graph(manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)`
call, not `run_subagent` — the ops domain's own `run_subagent` delegates
to `metrics-researcher`, restricted to `fetch_metrics_summary`/
`list_recent_incidents`, narrower than the full toolset this script's
open-ended questions need.

No durable checkpointer — a bare build_graph() defaults to an in-memory
MemorySaver, matching a "let me ask something" CLI: one question, one
answer, no memory expected across runs.

Run with: `python -m scripts.ops_investigate "why is latency high right now?"`
"""
import asyncio
import getpass
import sys
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.graph_build import build_graph
from app.core.config import DEFAULT_TENANT
from app.core.logging_config import configure_logging
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
    # (app/agent/graph.py); LangGraph's sync Pregel loop can't run one.
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


if __name__ == "__main__":
    configure_logging()
    question = " ".join(sys.argv[1:]) or "Is anything unusual right now?"
    print(asyncio.run(investigate(question)))
