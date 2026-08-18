"""Prometheus metrics for the agent runtime.

Exposed at `GET /metrics` (see app/api.py) for Prometheus to scrape. These
are process-local counters/histograms — the standard Prometheus pull model
aggregates across instances at the scraper/Grafana layer, not here. Rates
(human-approval rate, retry rate, p50/p95/p99 latency, etc.) are *derived*
from these at query time via PromQL (`rate()`, `histogram_quantile()`), not
stored directly.

Two ways these get incremented:
  - Tool calls/errors: via `MetricsCallbackHandler`, wired into
    `config["callbacks"]` in app/agent.py the same way the Langfuse handler
    is — so it observes every tool run without any instrumentation inside
    app/graph.py's node functions.
  - Everything else (retries, HITL decisions, budget trips, context-
    retrieval degradations, history compaction, request
    outcome/latency/iterations/tokens): incremented directly at the point
    the event happens, in app/graph.py's nodes and app/agent.py's turn
    boundary. These can happen more than once per turn (e.g. two HITL
    round-trips in one conversation turn) or need the final state dict
    (iterations, tokens) — both awkward to reconstruct reliably from
    generic callback events, so a direct `.inc()` is simpler and correct.
"""
from langchain_core.callbacks import BaseCallbackHandler
from prometheus_client import Counter, Histogram

agent_requests_total = Counter(
    "agent_requests_total", "Total agent turns by outcome", ["outcome"]
)  # outcome: success | rejected | error | timeout

agent_latency_seconds = Histogram(
    "agent_latency_seconds", "End-to-end latency per agent turn, in seconds"
)

# (.005, .01, .025, .05, .075, .1, .25, .5, .75, 1.0, 2.5, 5.0, 7.5, 10.0)

# agent_latency_seconds = Histogram(
#     "agent_latency_seconds", 
#     "End-to-end latency per agent turn, in seconds",
#     # Captures fast tool calls all the way up to a 2-minute timeout limit
#     buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0, 90.0, 120.0) 
# )

agent_iterations = Histogram(
    "agent_iterations",
    "LLM loop iterations per turn",
    buckets=(1, 2, 3, 4, 5, 7, 10, 15),
)

agent_tokens_total = Counter(
    "agent_tokens_total",
    "Cumulative tokens consumed, summed per turn from usage_metadata "
    "(0 if the model/proxy doesn't report it)",
)

agent_tool_calls_total = Counter(
    "agent_tool_calls_total", "Tool calls issued by the LLM", ["tool"]
)

agent_tool_errors_total = Counter(
    "agent_tool_errors_total", "Tool calls that raised and were recovered by handle_tool_errors"
)

agent_human_approval_total = Counter(
    "agent_human_approval_total", "HITL approval decisions", ["decision"]
)  # decision: approved | rejected

agent_retry_total = Counter(
    "agent_retry_total", "Output-quality retries triggered by route_after_check"
)

agent_tool_budget_exceeded_total = Counter(
    "agent_tool_budget_exceeded_total",
    "Turns where the LLM requested more tool calls at once than MAX_TOOL_CALLS_PER_TURN allows",
)

agent_token_budget_exceeded_total = Counter(
    "agent_token_budget_exceeded_total",
    "Turns cut short by MAX_TOKENS_PER_TURN",
)

agent_context_retrieval_degraded_total = Counter(
    "agent_context_retrieval_degraded_total",
    "retrieve_context calls that failed and degraded to no pre-fetched context",
)

agent_history_compacted_total = Counter(
    "agent_history_compacted_total",
    "Turns where conversation history was trimmed to MAX_HISTORY_TURNS",
)

agent_capability_gate_total = Counter(
    "agent_capability_gate_total",
    "Tool-call batches routed to human_approval because a tool's declared "
    "capability required it (mandatory), independent of require_approval (opt-in)",
    ["capability"],
)  # capability: mutating | outward

agent_checkpoint_issue_total = Counter(
    "agent_checkpoint_issue_total",
    "Resume attempts refused by resumability_error before Command(resume=...)",
    ["reason"],
)  # reason: checkpoint_lost | checkpoint_incompatible

agent_missing_ctx_total = Counter(
    "agent_missing_ctx_total",
    "Turns rejected by reject_context: no valid SecurityCtx (tenant+principal) "
    "was stamped on the request before it reached the graph",
)

agent_unattended_pause_total = Counter(
    "agent_unattended_pause_total",
    "Turns auto-declined by answer()/stream_turn() after pausing at a "
    "mandatory capability gate — these entry points are single-shot/one-way "
    "and have no way to solicit a real human decision (unlike "
    "astream_events_turn's approval_required/astream_events_resume flow)",
)


class MetricsCallbackHandler(BaseCallbackHandler):
    """Records tool-call and tool-error counts. Pass an instance in
    `config["callbacks"]` — LangChain fires these hooks for every tool run
    inside the graph's ToolNode, regardless of which tool or how many run
    in parallel."""

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:
        name = (serialized or {}).get("name", "unknown")
        agent_tool_calls_total.labels(tool=name).inc()

    def on_tool_error(self, error, **kwargs) -> None:
        agent_tool_errors_total.inc()
