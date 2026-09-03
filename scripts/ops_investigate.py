"""Ad-hoc ops investigation: ask the ops domain's own agent a one-off
question ("why is latency high right now?", "did anything break this
morning?") using its full toolset (see app/domains/ops/tools.py). Most of
an investigation stays read_only (fetch_metrics_summary,
list_recent_incidents) and never pauses — but if the model decides to
call a mutating tool (log_incident, resolve_incident, post_to_team_channel)
it WILL hit should_continue's mandatory human_approval gate same as any
other caller, and this one-shot invocation has no resume loop to answer
it: `investigate()` just returns whatever's in the last AIMessage at that
point (typically empty, since the pending tool call itself carries no
text). Disclosed rather than papered over — an interactive Telegram
session handles the same gate by actually presenting the pause to a human;
this CLI is for read-only questions, and a mutating one surfacing an empty
answer is the honest signal something needs a human in the loop instead.

A one-shot `build_graph(manifest=OPS_MANIFEST, domain=OPS_DOMAIN_PLUGIN)`
call, NOT `run_subagent` — even though the ops domain has its own
`run_subagent` today, resolved against its own tools
(app/agent/tools.py::make_domain_subagent_tool, not hardwired to Acme's
tool universe the way an earlier version of this domain was). The reason
to skip it here isn't a limitation, just a fit: `run_subagent` would
delegate to `metrics-researcher`, restricted to
`fetch_metrics_summary`/`list_recent_incidents` — narrower than this
script's own caller, which wants the ops domain's FULL toolset for an
open-ended question, this module's own docstring above included.

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
