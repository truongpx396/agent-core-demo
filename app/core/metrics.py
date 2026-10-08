"""OpenTelemetry metrics for the agent runtime.

Pushed via OTLP to a shared otel-collector (see telemetry.py::configure_telemetry),
exposing one aggregated Prometheus target covering the API and every scaled
agent_worker.py/ingest_worker.py replica (pattern 43) — a pull-based
`GET /metrics` on the API alone could never see a worker's metrics, since
nothing scrapes it directly. Rates are derived from these via PromQL in
Grafana, not stored directly.

`Counter`/`Histogram` below are a prometheus_client-shaped wrapper around
the real OTel API (`.add`/`.record`), so existing call sites
(`x.labels(k=v).inc()` / `x.observe(v)`) needed no rewrite — a library swap
under the hood only. Histogram bucket boundaries aren't set here (OTel has
no per-call bucket param) — they're Views on the MeterProvider in
telemetry.py, matched by instrument name.

Two ways these get incremented: tool calls/errors via
`MetricsCallbackHandler` (wired into `config["callbacks"]` like the
Langfuse handler, so it needs no instrumentation inside graph.py's nodes);
everything else incremented directly at the point it happens in graph.py's
nodes / runtime.py's turn boundary, since those events can fire more than
once per turn or need the final state dict — awkward to reconstruct from
generic callback events.
"""
import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import metrics as metrics_api

logger = logging.getLogger(__name__)

# A proxy meter (see telemetry.py): every create_counter/create_histogram
# call below is safe before configure_telemetry() runs, and gets replayed
# against the real MeterProvider once it does.
_meter = metrics_api.get_meter("app.core.metrics")


def _fingerprint(text: str) -> str:
    """A short, stable fingerprint of `text` for an audit log line — NEVER
    the raw content (pattern 14's "never message content in a generic log"
    rule, extended to tool args/results). Enough to correlate "was this the
    same result as last time" without an unscrubbed copy sitting outside
    Langfuse."""
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


class _BoundGauge:
    __slots__ = ("_instrument", "_attributes")

    # `Any`: this OpenTelemetry release exposes the synchronous Gauge only under a private name (`_Gauge`), while
    # `Meter.create_gauge` that returns it is public; naming the private class here would break on the next release.
    def __init__(self, instrument: Any, attributes: Mapping[str, str]):
        self._instrument = instrument
        self._attributes = attributes

    def set(self, value: float) -> None:
        self._instrument.set(value, attributes=self._attributes)


class Gauge:
    """prometheus_client.Gauge-shaped wrapper around an OTel synchronous Gauge: the LAST value set wins, which is what a
    "how old is the oldest unsent thing right now" reading needs and a Counter cannot say."""

    def __init__(self, name: str, description: str = "", unit: str = "", labelnames=()):
        self.name = name
        self._instrument = _meter.create_gauge(name, description=description, unit=unit)
        self._labelnames = tuple(labelnames)

    def labels(self, **kwargs: str) -> _BoundGauge:
        return _BoundGauge(self._instrument, kwargs)

    def set(self, value: float) -> None:
        self._instrument.set(value)


agent_requests_total = Counter(
    "agent_requests_total", "Total agent turns by outcome", ["outcome"]
)  # outcome: success | rejected | error | timeout | cancelled

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
    "(app/agent/graph_tools.py's invalid_tool_call node) — a model output-quality issue, not dispatched "
    "or surfaced to human_approval",
)

agent_use_skill_without_search_total = Counter(
    "agent_use_skill_without_search_total",
    "Turns where the LLM called use_skill without ever calling skill_search "
    "first in the same turn (app/agent/graph_skills.py's use_skill_without_search "
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
    "Pauses auto-declined by astream_events_turn_unattended (one count per "
    "decline round — a model that re-requests a declined write is declined "
    "again, up to UNATTENDED_MAX_DECLINE_ROUNDS) at "
    "a mandatory capability gate — its callers (app/job_queue/agent_worker.py's "
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
)  # outcome: allowed | blocked_injection | blocked_denylist | blocked_ml_injection |
# error (degraded — treated as allowed)

agent_worker_job_reclaimed_total = Counter(
    "agent_worker_job_reclaimed_total",
    "Jobs reclaimed via XAUTOCLAIM after their original worker died mid-job "
    "(app/job_queue/queue.py::reclaim_stale_entries). 'retried' means the "
    "job was judged safe to run again (app/job_queue/agent_worker.py::"
    "_classify_reclaimed_turn — an unfinished turn is continued from its "
    "checkpoint, not restarted) and was silently republished; "
    "'dead_lettered' means it was surfaced as an error on its own results "
    "stream and archived instead (an already-finished or approval-paused "
    "turn, an unreadable checkpoint, or retries exhausted), since "
    "re-running it could have duplicated an already-applied side "
    "effect. Any sustained rate here means workers are crashing, not that "
    "recovery is working as intended — 'retried' vs 'dead_lettered' tells "
    "you whether that crashing is at least self-healing.",
    ["queue", "outcome"],
)  # queue: agent | ingest; outcome: retried | dead_lettered

agent_tool_dedup_degraded_total = Counter(
    "agent_tool_dedup_degraded_total",
    "Mutating/outward tool calls where app/agent/tool_idempotency.py::idempotent "
    "couldn't reach its own dedup store (a connection failure, or a failure "
    "to persist the result after a successful call) and fell back to "
    "running the tool unprotected instead — same degrade-don't-fail-the-turn "
    "posture as agent_moderation_ml_degraded_total. A sustained rate here "
    "narrows the window in which a reclaimed 'resume' retry could duplicate "
    "a side effect, since dedup wasn't actually available to catch it.",
)

agent_moderation_ml_degraded_total = Counter(
    "agent_moderation_ml_degraded_total",
    "Turns where the ML injection-classifier layer (app/agent/moderation.py's "
    "call to ml-service's /prompt-guard) couldn't be reached and the turn "
    "proceeded on the pattern-based layer's result alone — same "
    "degrade-don't-fail-the-turn posture as agent_retrieval_degraded_total, "
    "counted separately from agent_moderation_total's own outcomes so a "
    "network blip against ml-service is distinguishable from a genuine "
    "'nothing flagged it' allow.",
)

agent_memory_deletion_total = Counter(
    "agent_memory_deletion_total",
    "Memory deletion calls by outcome (app/agent/memory.py)",
    ["outcome"],
)  # outcome: deleted | refused — never carries tenant/principal (see app/agent/memory.py's
# structured log line for the identified, per-call audit trail instead)

agent_no_progress_total = Counter(
    "agent_no_progress_total",
    "Turns ended early for repeating an identical tool-call batch "
    "MAX_REPEATED_ACTIONS times in a row (app/agent/graph_routing.py::should_continue)",
)

agent_cost_ceiling_exceeded_total = Counter(
    "agent_cost_ceiling_exceeded_total",
    "Turns ended early for exceeding MAX_COST_USD_PER_TURN (app/agent/graph_routing.py::should_continue)",
)

agent_cancellation_total = Counter(
    "agent_cancellation_total",
    "Paused runs cancelled via app/agent/runtime_stream.py::cancel_run (GRAPH_PATTERNS.md pattern 36)",
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

agent_budget_exceeded_total = Counter(
    "agent_budget_exceeded_total",
    "Turns refused before starting because a spend limit was already used up "
    "(app/agent/budgets.py::check_allowance); only the limit that refused is counted. "
    "Replaces agent_tenant_budget_exceeded_total, which could not say whose limit it was.",
    ["scope", "window"],
)  # scope: tenant | principal ; window: day | month

agent_budget_threshold_total = Counter(
    "agent_budget_threshold_total",
    "Turns that were still allowed but had crossed a warning threshold of a spend limit — "
    "only the highest crossed (70, 85 or 95 percent) is counted. An early signal before "
    "agent_budget_exceeded_total starts firing. Replaces agent_tenant_budget_warning_total.",
    ["scope", "window", "threshold"],
)  # threshold: "70" | "85" | "95"

agent_gateway_budget_exceeded_total = Counter(
    "agent_gateway_budget_exceeded_total",
    "LLM calls the gateway refused because the app's key reached its max_budget "
    "(app/agent/gateway.py). The gateway is the BACKSTOP: this firing means the app-level "
    "ceilings did not stop the spend first, or the backstop is sized too low.",
)

agent_unpriced_usage_total = Counter(
    "agent_unpriced_usage_total",
    "LLM calls that spent tokens on a model with no known price (app/agent/pricing.py) — "
    "every dollar ceiling reads $0 for that spend, so this must stay at zero in production",
    ["model_alias"],
)

agent_credit_overdraft_total = Counter(
    "agent_credit_overdraft_total",
    "Debits larger than the tenant's available credits (app/billing/credits.py): the model call "
    "had already happened, so the shortfall was booked as overdraft instead of refused. With "
    "gating on this should stay near zero; sustained growth means usage is outrunning the wallet "
    "(a missed top-up, or gating off).",
)

agent_credit_enforcement_refused_total = Counter(
    "agent_credit_enforcement_refused_total",
    "Turns refused before any model work because the tenant's wallet had no available credits "
    "(app/agent/budgets.py, ErrorCode.INSUFFICIENT_CREDITS). Normal operation for a prepaid tenant that "
    "has run out, so it is a rate to watch, not an alert. It has no tenant label (who is in the log line).",
)

agent_billing_webhook_total = Counter(
    "agent_billing_webhook_total",
    "Payment-provider webhook deliveries by outcome (POST /billing/webhooks/{provider}, app/billing/webhooks.py). "
    "`quarantined` means a customer PAID and was granted nothing and nobody is retrying: it pages "
    "(BillingWebhookQuarantined). `failed` means applying it raised and the provider will retry: it pages too "
    "(BillingWebhookFailing), because a sustained database fault would otherwise surface only as a provider's "
    "dashboard. `invalid_signature` is a forged or misconfigured delivery. `provider` is a configured adapter "
    "name or `unknown` (never the caller's own string: an unauthenticated route must not mint label values).",
    ["provider", "outcome"],
)  # outcome: applied | duplicate | ignored | quarantined | retry | failed | invalid_signature | invalid_payload | unknown_provider | too_large

agent_usage_export_total = Counter(
    "agent_usage_export_total",
    "Usage events handled by the export worker (app/billing/export.py), by outcome. `expired` means an event aged past "
    "BILLING_EXPORT_MAX_AGE_DAYS unsent (alert UsageExportExpired: usage the provider will never bill); `failed` means a "
    "permanent refusal, the attempt budget spent, or no customer link (alert UsageExportFailed); `retry` is a retryable "
    "failure that will back off and try again; `sent` includes a provider-reported duplicate, which is success.",
    ["provider", "outcome"],
)  # outcome: sent | retry | failed | expired

agent_usage_export_oldest_pending_age_seconds = Gauge(
    "agent_usage_export_oldest_pending_age_seconds",
    "Age of the oldest usage event still waiting to be exported, per provider, set each worker pass (0 when nothing is "
    "waiting). Alert UsageExportStuck at 7 days: well inside the age limit, so someone is told while the events are "
    "still sendable. Disclosed (corrected in PR 6, which verified it): a synchronous gauge is exported ONCE per set (the SDK "
    "clears it after collecting) and the collector's Prometheus exporter drops a series 5 minutes after its last update "
    "(`metric_expiration`), so a dead worker does not leave a stale value behind: the series DISAPPEARS and UsageExportStuck "
    "resolves on its own. The worker's own liveness is not alerted yet; Prometheus cannot tell a worker that never ran from one that died.",
    unit="s",
    labelnames=["provider"],
)

agent_credit_granted_total = Counter(
    "agent_credit_granted_total",
    "Credits added to wallets (app/billing/credits.py), by lot source: purchase | subscription | promo | manual | "
    "adjustment. Counted when the grant is applied, inside the caller's transaction, so one that later rolls back "
    "is over-counted: this is a rate for a dashboard, and the wallet's entries are the record.",
    ["source"],
)

agent_credit_debited_total = Counter(
    "agent_credit_debited_total",
    "Credits taken from wallets, by transaction kind: debit (a model call) | clawback (a refund) | adjust (an "
    "operator correction). Expiry is not here: it is credits that left unused, not credits used. Counted at apply "
    "time like agent_credit_granted_total, with the same caveat.",
    ["kind"],
)

agent_credit_outstanding = Gauge(
    "agent_credit_outstanding",
    "Credits held across every wallet, set by each reconciliation pass (app/billing/reconcile.py): `available` is what "
    "may still be consumed, `debt` is what overdraft lots owe (a positive number). Deliberately no tenant label: who "
    "holds what is `make credits ARGS='show --tenant ...'`. Only as fresh as the last pass.",
    labelnames=["state"],
)

agent_credit_reconcile_max_drift_usd = Gauge(
    "agent_credit_reconcile_max_drift_usd",
    "The largest per-tenant, per-day difference ABOVE tolerance between usage events and the ledger, the gateway spend log "
    "or the wallet, in USD, as of the last reconciliation pass (0 when everything agrees). No tenant label: the report "
    "(`make credit-reconcile`) names them. Alert CreditReconcileDrift. The worker re-sets it every minute because the "
    "collector forgets a series 5 minutes after its last update.",
    unit="USD",
)

agent_credit_reconcile_total = Counter(
    "agent_credit_reconcile_total",
    "Reconciliation passes by outcome: ok (everything agrees) | drift (something is above tolerance) | incomplete (the "
    "gateway had more rows than CREDIT_RECONCILE_GATEWAY_MAX_PAGES, so that comparison was skipped) | failed (the pass "
    "itself raised: the gateway or database was unreachable; alert CreditReconcileFailing).",
    ["outcome"],
)  # outcome: ok | drift | incomplete | failed

agent_cost_governance_degraded_total = Counter(
    "agent_cost_governance_degraded_total",
    "Cost-governance paths that failed and carried on instead of failing the turn "
    "(spec 008 A1)",
    ["path"],
)  # path: price_lookup | ledger_write | ledger_read | policy_read | reservation | model_resolve
# | usage_event_write | usage_event_table_missing | usage_event_identity | usage_missing (app/agent/usage_events.py)
# | export_enqueue (app/agent/usage_events.py: the event was kept, queuing it for export failed)
# | credit_debit (app/agent/usage_events.py: the event was kept, its debit failed) | credit_read (app/agent/budgets.py: the gate
# could not read the wallet)

agent_upload_rejected_total = Counter(
    "agent_upload_rejected_total",
    "POST /ingest/upload files rejected before any MinIO write",
    ["reason"],
)  # reason: bad_file_type | too_large | too_many_files

agent_upload_failed_total = Counter(
    "agent_upload_failed_total",
    "POST /ingest/upload files that failed AFTER an accepted upload attempt "
    "(MinIO write or job publish) — distinct from agent_upload_rejected_total, "
    "which is pre-write validation only",
    ["reason"],
)  # reason: storage_error

agent_worker_unreachable_total = Counter(
    "agent_worker_unreachable_total",
    "Queued jobs whose results stream received no event at all within the "
    "first-event deadline (app/job_queue/queue.py and app/ingestion/ingest_queue.py "
    "read_results) — nobody picked the job up. One of these is a stuck request; a "
    "stream of them is a worker pool that answers nothing (none running for a "
    "domain, or all of them dead), which until this metric produced one error per "
    "caller and no signal an alert could use",
    ["queue"],
)  # queue: agent | ingest

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

agent_tool_retry_total = Counter(
    "agent_tool_retry_total",
    "Retry attempts made by app/core/resilience.py::CircuitBreaker.call after a "
    "transient (retry_on-listed) failure — counted per retry, not per call, so this "
    "can run well ahead of agent_circuit_breaker_opened_total under a brief blip "
    "every retry recovers from before the breaker ever trips.",
    ["dependency"],
)  # dependency: opensandbox_mcp | crawl4ai

agent_circuit_breaker_opened_total = Counter(
    "agent_circuit_breaker_opened_total",
    "A CircuitBreaker (app/core/resilience.py) tripped open after failure_threshold "
    "consecutive calls to one dependency each exhausted their own retries — that "
    "dependency is presumed down, and every call to it fails fast with "
    "CircuitOpenError instead of retrying, until its cooldown elapses. A sustained "
    "rate here means a real outage of a shared local dependency (opensandbox-server, "
    "crawl4ai), not a per-tenant/per-request problem.",
    ["dependency"],
)

agent_circuit_breaker_rejected_total = Counter(
    "agent_circuit_breaker_rejected_total",
    "Calls rejected immediately with CircuitOpenError while a breaker was already "
    "open (including one already in CircuitState.HALF_OPEN, i.e. a trial call was "
    "already in flight) — each one is a call that would otherwise have paid the "
    "dependency's full connect-timeout for a failure the breaker already knows is "
    "coming, or piled a second concurrent probe onto a dependency still proving "
    "itself healthy.",
    ["dependency"],
)

agent_circuit_breaker_half_open_total = Counter(
    "agent_circuit_breaker_half_open_total",
    "A CircuitBreaker's cooldown elapsed and it admitted exactly one trial call "
    "(CircuitState.HALF_OPEN) to decide whether to fully close or re-open — every "
    "OTHER call arriving before that trial resolves is rejected instead of also "
    "being admitted (agent_circuit_breaker_rejected_total), so this dependency "
    "never gets hit with a burst the instant its cooldown expires.",
    ["dependency"],
)

agent_team_channel_notify_total = Counter(
    "agent_team_channel_notify_total",
    "app/domains/notify.py::post_to_team_channel attempts by sink and outcome. "
    "This is a PIVOT transaction (can't be un-sent) backing a human_approval'd "
    "escalation/handoff/incident-log write that already committed — a failed "
    "send here used to be a log line only (no metric, so no alert could ever "
    "fire on it), meaning a human could go unnotified with nothing surfacing "
    "that fact beyond logs nobody was watching. Still best-effort (the write "
    "it follows is the real source of truth, pullable via list/status tools "
    "even if every push fails) — this only makes 'pushes have been failing' "
    "an observable, alertable fact instead of a silent one.",
    ["sink", "outcome"],
)  # sink: local | slack — outcome: ok | error


class MetricsCallbackHandler(BaseCallbackHandler):
    """Records tool-call/tool-error counts AND a structured per-call audit
    line (pattern 37) — tool name, a fingerprint of args/result (never raw
    content), and LangChain's `run_id` correlating start to outcome. Pass an
    instance in `config["callbacks"]` — fires for every tool run in the
    graph's ToolNode.

    Logged, not durably stored: the reference design this mirrors implies a
    synchronous durable-store write gating dispatch, more audit
    infrastructure than this demo has. Logging is the honest scope here.
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
