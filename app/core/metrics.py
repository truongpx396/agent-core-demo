"""OpenTelemetry metrics for the agent runtime.

Pushed via OTLP to a shared otel-collector (docker-compose.observability.yml
— see app/core/telemetry.py::configure_telemetry, called once at real
process startup), which exposes one aggregated Prometheus scrape target
covering the API AND every independently-scaled app/turns/agent_worker.py /
app/ingestion/ingest_worker.py replica (GRAPH_PATTERNS.md pattern 43) — a
pull-based `GET /metrics` on the API process alone (this module's previous,
prometheus_client-backed design) could never see a worker's metrics at all,
since nothing scrapes a worker process directly. Rates (human-approval
rate, retry rate, p50/p95/p99 latency, etc.) are still *derived* from these
at query time via PromQL (`rate()`, `histogram_quantile()`) in Grafana, not
stored directly.

`Counter`/`Histogram` below are a small prometheus_client-shaped wrapper
around the real OTel API (`meter.create_counter`/`create_histogram`,
`.add`/`.record`) — every call site elsewhere in this app already uses
`some_counter.labels(k=v).inc()` / `some_histogram.observe(v)`
(prometheus_client's own surface), and keeping that shape here means this
was a genuine library swap under the hood, not a rewrite of every call
site across app/agent/, app/retrieval/, app/ingestion/, app/api/. Explicit
histogram bucket boundaries for agent_latency_seconds/agent_iterations
aren't set here — an OTel Histogram instrument has no per-call bucket
parameter — they're configured as Views on the MeterProvider in
app/core/telemetry.py, matched by instrument name.

Two ways these get incremented:
  - Tool calls/errors: via `MetricsCallbackHandler`, wired into
    `config["callbacks"]` in app/agent/runtime.py the same way the Langfuse handler
    is — so it observes every tool run without any instrumentation inside
    app/agent/graph.py's node functions.
  - Everything else (retries, HITL decisions, budget trips, context-
    retrieval degradations, history compaction, request
    outcome/latency/iterations/tokens): incremented directly at the point
    the event happens, in app/agent/graph.py's nodes and app/agent/runtime.py's turn
    boundary. These can happen more than once per turn (e.g. two HITL
    round-trips in one conversation turn) or need the final state dict
    (iterations, tokens) — both awkward to reconstruct reliably from
    generic callback events, so a direct `.inc()` is simpler and correct.
"""
import hashlib
import logging
from collections.abc import Mapping

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import metrics as metrics_api

logger = logging.getLogger(__name__)

# A proxy meter (verified empirically — see app/core/telemetry.py's module
# docstring): every create_counter/create_histogram call below is safe
# regardless of whether configure_telemetry() has run yet in this process,
# and gets transparently replayed against the real MeterProvider once it
# does.
_meter = metrics_api.get_meter("app.core.metrics")


def _fingerprint(text: str) -> str:
    """A short, stable fingerprint of `text` for an audit log line — NEVER
    the raw content itself (matching GRAPH_PATTERNS.md pattern 14's "never
    message content in a generic log" rule, extended here to tool call
    args/results specifically). Enough to correlate "was this the same
    result as last time" without the log line becoming a second,
    unscrubbed copy of prompt/document text sitting outside Langfuse."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


class _BoundCounter:
    __slots__ = ("_instrument", "_attributes")

    def __init__(self, instrument: metrics_api.Counter, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def inc(self, amount: float = 1) -> None:
        self._instrument.add(amount, attributes=self._attributes)


class Counter:
    """prometheus_client.Counter-shaped wrapper around an OTel Counter."""

    def __init__(self, name: str, description: str = "", labelnames=()):
        self.name = name  # read by tests/core/test_metrics.py to look up this
        # instrument's recorded data points via an InMemoryMetricReader —
        # OTel instruments don't expose their own name back once created.
        self._instrument = _meter.create_counter(name, description=description)
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundCounter:
        return _BoundCounter(self._instrument, kwargs)

    def inc(self, amount: float = 1) -> None:
        self._instrument.add(amount)


class _BoundHistogram:
    __slots__ = ("_instrument", "_attributes")

    def __init__(self, instrument: metrics_api.Histogram, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def observe(self, value: float) -> None:
        self._instrument.record(value, attributes=self._attributes)


class Histogram:
    """prometheus_client.Histogram-shaped wrapper around an OTel Histogram.
    Bucket boundaries live in app/core/telemetry.py's Views, not here."""

    def __init__(self, name: str, description: str = "", unit: str = "", labelnames=()):
        self._instrument = _meter.create_histogram(
            name, description=description, unit=unit
        )
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundHistogram:
        return _BoundHistogram(self._instrument, kwargs)

    def observe(self, value: float) -> None:
        self._instrument.record(value)


agent_requests_total = Counter(
    "agent_requests_total", "Total agent turns by outcome", ["outcome"]
)  # outcome: success | rejected | error | timeout

agent_latency_seconds = Histogram(
    "agent_latency_seconds",
    "End-to-end latency per agent turn, in seconds",
    unit="s",
)

agent_iterations = Histogram(
    "agent_iterations",
    "LLM loop iterations per turn",
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

agent_zero_citations_total = Counter(
    "agent_zero_citations_total",
    "Non-empty final answers with retrieved context available but zero "
    "citation markers used (check_output) — the opposite failure mode "
    "from ungrounded_claims_count (citing something not backed by a "
    "source, vs. using sourced content without citing it at all). "
    "Directional only, like ungrounded_claims_count: a legitimate "
    "general-knowledge or calculator-only answer looks identical, since "
    "retrieve_context always returns its top-K docs regardless of "
    "relevance — not acted on by route_after_check, just observed.",
)

agent_misattributed_citations_total = Counter(
    "agent_misattributed_citations_total",
    "Final answers where a real, in-range citation marker was used on a "
    "sentence whose content shares no meaningful vocabulary with that "
    "marker's actual source text (check_output's likely_misattributed_"
    "citations) — a real marker attached to unsupported content, as opposed "
    "to a fabricated marker (ungrounded_claims_count) or a real source used "
    "with no marker at all (the opposite failure, likely_uncited_citations). "
    "Acted on by route_after_check: unlike zero_citations/ungrounded_claims, "
    "this one triggers a retry.",
)

agent_reference_footer_stripped_total = Counter(
    "agent_reference_footer_stripped_total",
    "Final answers where check_output stripped a fabricated markdown-style "
    "reference-list footer ('[1]: some link') the model appended after its "
    "own inline [n] markers (see _strip_fabricated_reference_footer) — this "
    "app's citation convention is inline-only, so any such footer's 'link' "
    "is always invented, never a real source the model was actually given.",
)

agent_citation_auto_inserted_total = Counter(
    "agent_citation_auto_inserted_total",
    "Final answers where check_output mechanically inserted a missing [n] "
    "marker into the specific sentence likely_uncited_citations flagged, "
    "instead of retrying the model over it (see "
    "_insert_missing_citation_markers's own docstring) — live-verified "
    "that asking the model to fix this itself (the standard reminder, six "
    "reworded variants, and the actual concrete retry-feedback message) "
    "reliably does not work on a real case, so this is the primary "
    "correction path for likely_uncited_citations now, not a fallback. "
    "Watch this alongside likely_uncited_citations firing at all: a rising "
    "rate here means the model is drifting toward citing less, even though "
    "each individual case still gets silently corrected.",
)

agent_deferred_instead_of_acting_total = Counter(
    "agent_deferred_instead_of_acting_total",
    "Final answers that narrate an intent to use a tool ('I will use the X "
    "tool...') or ask the user's permission to proceed ('would you like me "
    "to?') instead of actually calling the tool or answering directly "
    "(check_output's _defers_instead_of_acting) — real bug, found live: a "
    "3B model repeating this across several turns on the same thread, "
    "each 'yes' reply just restarting the identical unresolved cycle since "
    "no real tool_calls were ever made. Acted on by route_after_check: "
    "triggers a retry with feedback to call the tool now, not describe it.",
)

agent_fabricated_tool_output_total = Counter(
    "agent_fabricated_tool_output_total",
    "Final answers containing two or more markdown code fences with NO "
    "real tool_calls entry backing them (check_output's "
    "_fabricates_tool_output) — a script AND a plausible-looking 'output' "
    "for it, presented as if run_command_in_sandbox had actually run, when "
    "it never did. Real bug, found live: after a sandbox approval was "
    "declined once, the model invented both a script and its output, whose "
    "own fabricated arithmetic didn't even match its own fabricated code. "
    "Acted on by route_after_check: triggers a retry with feedback that "
    "the tool was never actually called.",
)

agent_skipped_required_tool_total = Counter(
    "agent_skipped_required_tool_total",
    "Final answers where a skill was loaded this turn (use_skill) whose "
    "own text names run_command_in_sandbox as required, but that tool was "
    "never actually called, even though the final answer states a "
    "specific dollar figure (check_output's "
    "_skipped_required_sandbox_after_skill). Real bug, found live: the "
    "deal-economics skill's own text says not to estimate this kind of "
    "number by hand, and the model estimated it by hand anyway — correct "
    "that one specific time, but nothing enforced it, and every other "
    "live freehand attempt at the same math landed on a wrong number. "
    "Acted on by route_after_check: triggers a retry with feedback to "
    "actually call the tool the skill named.",
)

agent_system_prompt_leak_total = Counter(
    "agent_system_prompt_leak_total",
    "Final answers containing a long, verbatim run of the seeded system "
    "prompt's own text (check_output's _leaks_system_prompt) — output-side "
    "defense-in-depth alongside app/agent/moderation.py's input-side "
    "screening: an injection phrased in a way moderation's known-pattern "
    "regexes don't catch can still be caught here if it actually succeeds "
    "in getting the model to recite its instructions back. Acted on by "
    "route_after_check: triggers a retry, same as the other "
    "check_output-computed rejection reasons.",
)

agent_retry_exhausted_total = Counter(
    "agent_retry_exhausted_total",
    "Turns that gave up on retry_output's repair loop because the SAME "
    "check_output rejection reason (too_short/deferred/uncited/"
    "misattributed) fired on MAX_CONSECUTIVE_SAME_RETRY_REASON consecutive "
    "rounds — the model wasn't converging, just repeating. Routes to "
    "retry_exhausted instead of another retry_output round: real bug, "
    "found live — a stuck deferral loop burned 6 full retry rounds and "
    "~18k tokens before should_continue's own (much blunter) "
    "MAX_TOKENS_PER_TURN cap finally cut it off, landing on the exact same "
    "outcome this metric's own routing now reaches in 2 rounds.",
)

agent_tool_budget_exceeded_total = Counter(
    "agent_tool_budget_exceeded_total",
    "Turns where the LLM requested more tool calls at once than MAX_TOOL_CALLS_PER_TURN allows",
)

agent_invalid_tool_call_total = Counter(
    "agent_invalid_tool_call_total",
    "Turns where the LLM emitted a tool_call whose name isn't a real registered tool "
    "(app/agent/graph.py's invalid_tool_call node) — a model output-quality issue, not dispatched "
    "or surfaced to human_approval",
)

agent_use_skill_without_search_total = Counter(
    "agent_use_skill_without_search_total",
    "Turns where the LLM called use_skill without ever calling skill_search "
    "first in the same turn (app/agent/graph.py's use_skill_without_search "
    "node, see _use_skill_called_without_search) — rejected and looped back "
    "to agent instead of dispatched, so a fabricated skill name never gets "
    "as far as a 'not found' failure the model might narrate into the final "
    "answer. A rising rate here is a real signal the model is reaching for "
    "a packaged skill it shouldn't (SYSTEM_PROMPT's skill_search trigger "
    "over-firing on a plain 'how do I build X' question, say), not just a "
    "quality nitpick.",
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
    "Turns where conversation history was trimmed after crossing "
    "HISTORY_TOKEN_CEILING, down to HISTORY_TOKEN_FLOOR",
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
    "Turns auto-declined by astream_events_turn_unattended after pausing at "
    "a mandatory capability gate — its callers (app/turns/agent_worker.py's "
    "queue consumer, app/channels/telegram.py) have no interactive human on "
    "the other end of the call to solicit a real decision from (unlike "
    "astream_events_turn's approval_required/astream_events_resume flow)",
)

agent_retrieval_degraded_total = Counter(
    "agent_retrieval_degraded_total",
    "Hybrid retrieval stages that failed and degraded (app/retrieval/qdrant_store.py::hybrid_search)",
    ["stage"],
)  # stage: sparse (-> dense-only) | rerank (-> RRF-fused order)

agent_semantic_cache_total = Counter(
    "agent_semantic_cache_total",
    "Semantic cache lookups by outcome (app/retrieval/semantic_cache.py)",
    ["outcome"],
)  # outcome: hit | miss | error (degraded — treated as a miss, recorded separately)

agent_ingest_total = Counter(
    "agent_ingest_total",
    "Successful ingest_text calls by source kind (app/ingestion/ingestor.py)",
    ["source"],
)  # source: text | file | url

agent_ingest_refused_total = Counter(
    "agent_ingest_refused_total",
    "Refused ingest attempts by reason (app/ingestion/ingestor.py)",
    ["reason"],
)  # reason: no_ctx | bad_file_type | ssrf_blocked | fetch_failed | too_large

agent_moderation_total = Counter(
    "agent_moderation_total",
    "Input moderation screens by outcome (app/agent/moderation.py)",
    ["outcome"],
)  # outcome: allowed | blocked_injection | blocked_denylist | error (degraded — treated as allowed)

agent_memory_deletion_total = Counter(
    "agent_memory_deletion_total",
    "Memory deletion calls by outcome (app/agent/memory.py)",
    ["outcome"],
)  # outcome: deleted | refused — never carries tenant/principal (see app/agent/memory.py's
# structured log line for the identified, per-call audit trail instead)

agent_no_progress_total = Counter(
    "agent_no_progress_total",
    "Turns ended early for repeating an identical tool-call batch "
    "MAX_REPEATED_ACTIONS times in a row (app/agent/graph.py::should_continue)",
)

agent_cost_ceiling_exceeded_total = Counter(
    "agent_cost_ceiling_exceeded_total",
    "Turns ended early for exceeding MAX_COST_USD_PER_TURN (app/agent/graph.py::should_continue)",
)

agent_cancellation_total = Counter(
    "agent_cancellation_total",
    "Paused runs cancelled via app/agent/runtime.py::cancel_run (GRAPH_PATTERNS.md pattern 36)",
)

agent_streaming_cancellation_total = Counter(
    "agent_streaming_cancellation_total",
    "Actively-streaming (not paused) turns stopped via app/agent/runtime.py's cancel_check "
    "mechanism — the POST /chat/cancel queued-path counterpart to agent_cancellation_total, "
    "for a turn that wasn't paused at human_approval when the stop was requested",
)

agent_context_window_exceeded_total = Counter(
    "agent_context_window_exceeded_total",
    "Turns ended at the context_window_exceeded terminal node: the cumulative "
    "history_summary stayed over MAX_HISTORY_SUMMARY_CHARS even after "
    "compact_history just updated it (app/agent/graph.py::route_after_compaction, "
    "GRAPH_PATTERNS.md pattern 41)",
)

agent_rate_limit_exceeded_total = Counter(
    "agent_rate_limit_exceeded_total",
    "HTTP requests rejected by app/api/main.py's per-tenant rate limiter (RATE_LIMIT_PER_MINUTE)",
)

agent_tenant_budget_exceeded_total = Counter(
    "agent_tenant_budget_exceeded_total",
    "Turns refused before starting because this tenant's rolling 24h spend already "
    "reached MAX_COST_USD_PER_TENANT_PER_DAY (app/agent/runtime.py::_tenant_over_daily_budget)",
)

agent_tenant_budget_warning_total = Counter(
    "agent_tenant_budget_warning_total",
    "Turns that proceeded but crossed 80% of MAX_COST_USD_PER_TENANT_PER_DAY — "
    "an early signal before agent_tenant_budget_exceeded_total starts firing",
)

agent_upload_rejected_total = Counter(
    "agent_upload_rejected_total",
    "POST /ingest/upload files rejected before any MinIO write",
    ["reason"],
)  # reason: bad_file_type | too_large

agent_subagent_run_total = Counter(
    "agent_subagent_run_total",
    "run_subagent calls (app/agent/tools.py, GRAPH_PATTERNS.md pattern 46) by "
    "subagent and outcome",
    ["subagent", "outcome"],
)  # outcome: completed | budget_exceeded | timeout | error

agent_subagent_duration_seconds = Histogram(
    "agent_subagent_duration_seconds",
    "Wall-clock duration of one nested subagent run, in seconds",
    unit="s",
    labelnames=["subagent"],
)


class MetricsCallbackHandler(BaseCallbackHandler):
    """Records tool-call/tool-error counts AND a structured per-call audit
    line (GRAPH_PATTERNS.md pattern 37) — tool name, a fingerprint (never
    the raw content) of the args and result, and LangChain's own `run_id`
    as the correlation key tying a call's start to its outcome. Pass an
    instance in `config["callbacks"]` — LangChain fires these hooks for
    every tool run inside the graph's ToolNode, regardless of which tool
    or how many run in parallel.

    Logged, not durably stored: the reference design this mirrors says "a
    tool call that cannot be audited MUST NOT run," which strictly implies
    a synchronous durable-store write gating dispatch — more audit
    infrastructure than this demo has. Logging is the honest scope here,
    not a hollow claim of that stronger guarantee.
    """

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs) -> None:
        name = (serialized or {}).get("name", "unknown")
        agent_tool_calls_total.labels(tool=name).inc()
        logger.info(
            "tool_called",
            extra={
                "tool": name,
                "run_id": str(run_id),
                "args_fingerprint": _fingerprint(input_str),
            },
        )

    def on_tool_end(self, output, *, run_id, **kwargs) -> None:
        result_text = getattr(output, "content", output)
        logger.info(
            "tool_succeeded",
            extra={
                "run_id": str(run_id),
                "result_fingerprint": _fingerprint(str(result_text)),
            },
        )

    def on_tool_error(self, error, *, run_id, **kwargs) -> None:
        agent_tool_errors_total.inc()
        logger.warning(
            "tool_failed",
            extra={"run_id": str(run_id), "error_class": type(error).__name__},
        )
