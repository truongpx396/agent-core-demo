"""Ad-hoc ops investigation: ask the ops domain's own agent a one-off
question ("why is latency high right now?", "did anything break this
morning?") using its full toolset (fetch_metrics_summary, read_only —
see app/domains/ops/tools.py).

A one-shot `build_graph(manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)`
call, NOT `run_subagent`: today's subagent registry
(app/agent/tools.py::_SUBAGENT_REGISTRY) is hardwired to
`app.agent.tools.TOOLS`/`TOOL_CAPABILITIES` — the Acme tool universe — so a
subagent declaring an ops-domain tool would have it silently dropped at
catalog-build time (app/agent/tools.py::_resolve_subagent_tools). Making
that registry domain-aware is a real, disclosed limitation this script
works around rather than papers over: an operator asking an open-ended
question wants the ops domain's FULL toolset anyway, which is arguably a
better fit than a read-only-restricted subagent would be regardless.

No durable checkpointer — each invocation is independent (a bare
build_graph() call defaults to an in-memory MemorySaver), matching how a
person actually uses a "let me ask something" CLI: one question, one
answer, no expectation it remembers the last run.

Run with: `python -m scripts.ops_investigate "why is latency high right now?"`
"""
import getpass
import sys
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.graph import build_graph
from app.core.config import DEFAULT_TENANT
from app.core.logging_config import configure_logging
from app.core.security import SecurityCtx
from app.domains.ops.domain import OPS_DOMAIN_PLUGIN, OPS_MANIFEST

# Local dev ctx, same shape as scripts/hitl_demo.py's _LOCAL_CTX — this
# process itself is the trusted boundary a real deployment's auth gateway
# would otherwise stamp.
_LOCAL_CTX: SecurityCtx = {
    "tenant": DEFAULT_TENANT,
    "principal": f"local:{getpass.getuser()}",
    "claims": {},
}


def investigate(question: str) -> str:
    graph = build_graph(manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)
    config = {
        "configurable": {
            "thread_id": f"ops-investigate:{uuid.uuid4().hex[:8]}",
            "ctx": _LOCAL_CTX,
        }
    }
    result = graph.invoke(
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
    print(investigate(question))
