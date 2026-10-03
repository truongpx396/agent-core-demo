# Implementation Plan: Core RAG Agent Turn Pipeline

**Branch**: `001-core-rag-agent-turn` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-core-rag-agent-turn/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

One LangGraph `StateGraph` runs every conversation turn: validate identity and input, compact
over-long history, screen the message, consult a semantic answer cache, pre-fetch cited context
from a hybrid (dense + BM25, RRF-fused, cross-encoder re-ranked) vector search, then loop
`agent` ⇄ `tools` under a layered set of safety budgets, and finally gate the candidate answer
through `check_output` (with a bounded repair/retry loop) before suggesting follow-ups and
writing the cache. State is checkpointed in Postgres so a turn can pause, restart and resume on
any process. Events are translated into a small typed stream (`token`, `tool_start`, `tool_end`,
`citations`, `followups`, `retry`, `compacted`, `done`, `approval_required`, `error`) that the
HTTP layer relays as SSE.

Approach in one line: *make every step of the pipeline an explicit, individually testable node
or edge function, and make every failure mode a named, counted, bounded state* — see
[research.md](./research.md) for the decision log and the real bug behind each choice.

## Technical Context

**Language/Version**: Python 3.13 (`Dockerfile` `python:3.13-slim`; CI matches)

**Primary Dependencies**: `langgraph==0.2.76`, `langchain-core==0.3.86`, `langchain-openai`
(pointed at the LiteLLM proxy), `langgraph-checkpoint-postgres==2.0.25`, `qdrant-client==1.19.0`,
`fastembed==0.8.0` (BM25 sparse leg), `psycopg[binary,pool]==3.3.5`, `redis==8.1.0`,
`fastapi==0.141.1`, `pydantic` v2 + `pydantic-settings`, `tiktoken` (approximate history budget),
`httpx`, `structlog`, OpenTelemetry SDK + OTLP/HTTP exporter, `langfuse` v2 (tracing).
Versions are the resolved pins in `requirements-lock.txt`; `requirements.txt` carries the reason
comments.

**Storage**: Postgres `checkpointer` DB (graph checkpoints, schema owned by
`AsyncPostgresSaver.setup()`); Postgres `appdata` DB (`chat_sessions`, `usage_ledger`, …) via the
pooled `app/agent/sql_store.py`; Qdrant collection `docs` (documents + memories, hybrid
dense/sparse vectors); Redis Stack (semantic answer cache, RediSearch KNN index
`idx:semantic_cache`).

**Testing**: pytest (`asyncio_mode=auto`, `-n auto`) in six tiers — hermetic default
(`make test`: fake LLM, mocked stores), `integration` (testcontainers Postgres/Redis/Qdrant),
`llm`/`e2e` (`make test-live`: real Ollama + Playwright), `promptfoo`, `garak`, `deepeval`,
and `make eval` (golden set, 5 repetitions, ≥95% grounded markers). Gates: `make lint` (ruff),
`make typecheck` (mypy over `app/` and `scripts/`), `make test`.

**Target Platform**: Linux containers (`docker-compose.yml`); developed on macOS with a *native*
Ollama (GPU) reached through LiteLLM aliases `chat` (`ollama_chat/qwen2.5:3b`, `num_ctx` 32000),
`embed` (`nomic-embed-text`), `vision` (slot only). Fully offline-capable.

**Project Type**: Web service + queue workers + CLI (Python monorepo; the graph is a library
shared by all three front ends).

**Performance Goals**: No latency SLO is asserted. Concurrency is a *measured* property:
per-process concurrent turns = `AGENT_WORKER_MAX_CONCURRENCY` (default 10); checkpoint I/O is
serialized per saver instance (measured ≈5× latency growth N=1→N=50 before the semaphore
workaround), so throughput scales with worker replicas — see `WORKER_CONCURRENCY.md`.

**Constraints**: Turn wall-clock 60 s (`REQUEST_TIMEOUT_SECONDS`); tool 15 s
(`TOOL_TIMEOUT_SECONDS`); rounds 10; tool fan-out 5; repeats 3; tokens 16 000; cost $0.50
(`MAX_COST_USD_PER_TURN`); history 24 000 → 4 000 estimated tokens; summary 4 000 chars;
cache similarity 0.95 / TTL 3 600 s; relevance floor −8.0 on the cross-encoder logit;
`RECURSION_LIMIT = MAX_ITERATIONS*2 + 15`. All but the hardcoded loop-count safety nets are
`Settings`-backed (`app/core/config.py`, `.env.example`).

**Scale/Scope**: Demo/teaching scale with production-shaped seams. Exercised at 250 concurrent
queued turns across 5 real worker processes by `tests/integration/test_worker_scaling.py` (real
Postgres + Redis; `loadtest/fake_llm_server.py` stands in for the model, because a native Ollama
serializes to one generation and would measure Ollama, not this app). One process serves exactly
one domain (domain composition is out of scope for the first spec batch).

**Unknowns**: none — retrospective, every value above is read from the repository.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (see end of section).*

Evidence is a repo path (and the test that pins it). "Owner" marks a principle whose primary
spec is another feature; this feature only has to *not break* it.

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | Yes — entry gate | **PASS** (owner: 002) | `validate_input` stamps `ctx` once from `config["configurable"]["ctx"]` only (`app/agent/graph.py`); `route_after_validation` checks `valid_ctx` *before* the empty-input check and routes to `reject_context`, which bumps `agent_missing_ctx_total`. Tests: `tests/agent/test_routing.py::TestRouteAfterValidationCtx`. |
| II | Mandatory approval (NN) | Yes — `should_continue` routing | **PASS** (owner: 003) | Any non-`read_only` pending call routes to `human_approval` regardless of `require_approval`; undeclared tool ⇒ `outward` (`graph_loop_guards.py::_mandatory_gate_reason`). Tests: `TestShouldContinueMandatoryGate`. |
| III | Fixed, typed tools | Yes — `search_docs`, `calculator`, `query_employees`, `ask_clarification` | **PASS** | Pydantic `args_schema` on each (`app/agent/tools.py`); calculator walks an AST allow-list, never `eval`; closed enums `Topic`/`Department`. |
| IV | Exactly-once side effects (NN) | Marginal — retry policy only | **PASS** (owner: 003) | `AGENT_RETRY_POLICY = RetryPolicy(max_attempts=3)` uses LangGraph's default `retry_on`, which excludes programming errors, and is attached only to `agent` (no side effect). Nothing in this feature performs a write. |
| V | Bounded, observable failure | Yes — the heart of this feature | **PASS with 2 advisories** *(the bug B1 below was fixed in #62)* | Every loop/wait has a ceiling (spec FR-013–FR-019); every degrade path increments a counter (`app/core/metrics.py`, e.g. `agent_context_retrieval_degraded_total`, `agent_retrieval_degraded_total{stage}`, `agent_semantic_cache_total{outcome}`); `# noqa: BLE001` carries a reason at each broad `except` (`graph_retrieval.py`, `qdrant_store.py`, `moderation.py`). **Advisory A1**: no alert rule on `agent_context_retrieval_degraded_total` or `agent_moderation_ml_degraded_total` — within the letter of V (neither leaves a human unaware of *committed business state*) but the second is a security control failing open silently. **Advisory A2** (checked against every `"type": "error"` emitter): six `ErrorCode` members are registered but never emitted, and two `error` paths bypass the envelope — a refused resume (`runtime_stream.py::astream_events_resume`) and the first-event deadline (`queue.py::read_results`); a third, the worker catch-all that forwarded raw `str(exc)`, was fixed in #64. The constitution says caller-facing errors MUST use the envelope, so A2 is a literal deviation, not just an advisory. **Bug B1** (verified by reproduction): `subagent_spend` is declared with an `operator.add` reducer, so `validate_input`'s per-turn reset to `[]` is a no-op and earlier turns' delegated spend keeps counting against later turns' token/cost ceilings — an unbounded-in-lifetime counter where the constitution wants a per-turn ceiling. |
| VI | Untrusted content is data | Yes | **PASS with 1 deviation** | `<retrieved_document>` framing + standing rule (`graph_agent_node.py`, `SYSTEM_PROMPT`); credential scrubbing at the single tool chokepoint (`_arun_with_timeout` → `app/core/scrubbing.py`); `SYSTEM_PROMPT` ctx-free (`tests/agent/test_prompt_cache_stability.py`); citations computed from output, never self-report (`graph_citations.py::_used_citations`). **Deviation D1**: moderation does *not* precede every model call — `compact_history` runs first and may call the model; see Complexity Tracking. |
| VII | Test discipline | Yes | **PASS with 1 advisory (A3)** | Default tier is hermetic through four autouse guards in `tests/conftest.py` (`mock_search_docs`, `mock_semantic_cache`, `mock_ml_moderation`, `mock_appdata_postgres`; the first two patch `_default_search` / `_default_cache_get/_set` on the live `graph` module); marked tiers self-skip. Checkpointer behavior is proven against a real `AsyncPostgresSaver` (`tests/agent/test_durable_checkpoint.py`, `integration` marker) and a real HITL pause/resume round trip at scale (`tests/integration/test_worker_scaling.py`). The constitution's known gap (fake-cursor SQL store tests) concerns the write stores in feature 003, not anything this feature owns. **Advisory A3 — closed by #68 and #69** (kept for the record; originally downgraded after verification): the cache's real RediSearch behavior *is* tested for the tenant axis — `tests/agent/test_concurrent_turns.py::TestSemanticCacheIsolationUnderConcurrency` (`integration` tier, real Redis Stack, hyphenated tenant names `tenant-a`/`tenant-b`, so a regression of `_escape_tag` would fail it). What is missing: nothing *names* the `_escape_tag` guarantee, there is no hermetic test of it, and the **principal** axis has no test. |
| VIII | Why-first docs, honest gaps | Yes | **PASS** | Patterns 1–7, 10–14, 16, 19–20, 22, 25–26, 30–32, 34–35, 39–41 in `GRAPH_PATTERNS.md` each carry the motivating bug; gaps are in *Extending Further* and, now, spec *Known gaps*. |

**Gate result (pre-research)**: no unjustified violation → proceed. D1 is recorded below, not
silently accepted.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: *changed* — Phase 1
verification found two things the pre-research pass did not. (1) **B1**: running a hermetic
reproduction while writing `data-model.md` showed `subagent_spend`'s per-turn reset is a no-op
(Principle V: a ceiling that silently becomes lifetime-scoped). (2) **A2 widened** while writing
`contracts/error-envelope.md`: six codes are unemitted and three `error` paths bypass the envelope,
one forwarding raw `str(exc)`. D1 is unchanged and is observable to a caller only as a `compacted`
event preceding a refusal on an over-ceiling conversation. No *design* decision in this plan
introduces a new violation; B1 and A2 are pre-existing defects found by verifying the as-built
system, which is the point of a retrospective plan. Gate result: **proceed, with B1/D1/A1/A2
recorded as open follow-ups (tasks.md).**

## Project Structure

### Documentation (this feature)

```text
specs/001-core-rag-agent-turn/
├── plan.md                      # This file
├── spec.md                      # /speckit-specify output
├── research.md                  # Phase 0 — decisions, each with its motivating bug
├── data-model.md                # Phase 1 — State fields, resets, node/edge topology, transitions
├── quickstart.md                # Phase 1 — runnable validation scenarios
├── contracts/
│   ├── chat-turn-http.md        # POST /chat/stream/queued + health + usage-free surface
│   ├── turn-event-stream.md     # the typed SSE event vocabulary and terminal-event rules
│   └── error-envelope.md        # {code, message, details} and the code registry
├── checklists/
│   └── requirements.md          # spec quality checklist
└── tasks.md                     # /speckit-tasks output (retrospective: built tasks pre-checked)
```

### Source Code (repository root)

```text
app/
├── agent/                       # the graph and its runtime
│   ├── graph.py                 # State, SYSTEM_PROMPT, safety-budget constants, entry/gating nodes,
│   │                            #   GraphDeps, _assemble_shared_graph_parts, STATE_SCHEMA_VERSION
│   ├── graph_build.py           # build_graph(): node registration (wrapped in _instrumented), edges
│   ├── graph_routing.py         # should_continue, check_output, route_after_check
│   ├── graph_loop_guards.py     # tool-call fingerprinting, repeat count, mandatory-gate reason
│   ├── graph_citations.py       # used/ungrounded/uncited/misattributed + marker auto-insert
│   ├── graph_output_guardrails.py  # deferral / fabrication / prompt-leak / skipped-tool heuristics
│   ├── graph_retry.py           # retry_output, retry_exhausted, no_answer fallback
│   ├── graph_retrieval.py       # retrieve_context node factory (degrades, never fails the turn)
│   ├── graph_cache.py           # check/write semantic-cache nodes
│   ├── graph_compaction.py      # token estimate, whole-turn trim, summary node, breadcrumb marker
│   ├── graph_agent_node.py      # the LLM-calling node; anchors summary/context before the question
│   ├── graph_followups.py       # suggest_followups
│   ├── graph_tools.py           # invalid_tool_call / too_many_tool_calls, _reject_tool_calls
│   ├── graph_utils.py           # _instrumented, _friendly_tool_error, _make_llm
│   ├── graph_hitl.py            # human_approval + resumability checks (feature 003 owns behavior)
│   ├── moderation.py            # pattern screen + Prompt Guard classifier client
│   ├── runtime.py               # checkpointer pool, graph singleton, seeding, tenant budget
│   ├── runtime_stream.py        # astream_events_turn, _run_graph_stream, resume/continue/cancel
│   ├── sessions.py              # chat_sessions directory (scoping detail → feature 002)
│   ├── usage_ledger.py          # per-turn usage + price table shared with the cost ceiling
│   └── tools.py                 # search_docs, calculator, ask_clarification, TOOL_CAPABILITIES
├── retrieval/
│   ├── qdrant_store.py          # hybrid_search: dense+sparse prefetch, RRF, rerank, degrade
│   ├── embeddings.py            # embed_text / embed_sparse / rerank (ml-service HTTP)
│   └── semantic_cache.py        # Redis Stack KNN cache, tenant+principal scoped
├── core/
│   ├── config.py                # Settings → module constants
│   ├── errors.py                # ErrorCode, ErrorEnvelope, TurnCancelled
│   ├── metrics.py               # OTel-backed counters/histograms (explicit buckets)
│   ├── scrubbing.py             # credential redaction chokepoint
│   └── logging_config.py        # structlog JSON, run_id correlation
└── api/
    ├── main.py                  # /chat/stream/queued SSE relay (transport detail → later feature)
    └── schemas.py               # ChatRequest etc.

postgres-init/
├── 05-checkpointer-db.sql       # CREATE DATABASE checkpointer (schema owned by saver.setup())
└── 06-chat-sessions.sql         # session directory for the switcher

tests/
├── agent/                       # test_routing, test_safety_budgets, test_nodes, test_agent_node,
│                                #   test_graph_integration, test_streaming_terminal_events,
│                                #   test_durable_checkpoint, test_moderation, test_prompt_cache_stability
├── retrieval/test_qdrant_store.py
├── core/                        # test_errors, test_metrics, test_scrubbing, test_logging_config
├── live/                        # real-model tests incl. test_prompt_injection_via_retrieval.py
└── conftest.py                  # autouse mocks that keep the default tier hermetic

scripts/eval.py                  # golden-set gate: 5 repetitions, 80% / 95% thresholds
```

**Structure Decision**: Single Python package with the graph as a library. The 15 `graph_*.py`
files are *file-size splits of one topology*, not separate components: `graph.py` holds `State`
and the constants so the siblings import one way, and the three symbols tests monkeypatch
(`_default_search`, `_default_cache_get/_set`, `interrupt`) deliberately stay there and are read
as `graph_module.X` (see the module docstrings). The compaction → moderation order is a
topology choice recorded as D1.

## Graph topology (as built)

```text
START → validate_input ─┬─ ctx invalid ──────────► reject_context ─► END
                        ├─ no text/image ────────► reject_input ───► END
                        └─ ok ─► compact_history ─┬─ summary too big ► context_window_exceeded ─► END
                                                  └─ ok ─► moderate_input ─┬─ blocked ► reject_moderation ─► END
                                                                           └─ ok ─► check_semantic_cache
check_semantic_cache ─┬─ hit ──────────────────────────────────────────────────────► check_output
                      └─ miss ─► retrieve_context ─► agent ─► (should_continue)
should_continue ─┬─ iteration/token/cost ceiling, or 3 identical batches ──► no_answer ─► END
                 ├─ no tool calls ──────────────────────────────────────────► check_output
                 ├─ >5 tool calls ──► too_many_tool_calls ─► agent
                 ├─ unknown tool ───► invalid_tool_call ───► agent
                 ├─ use_skill w/o skill_search ► use_skill_without_search ► agent
                 ├─ non-read_only, or require_approval ► human_approval ─┬─ approved ► tools ─► agent
                 │                                                        ├─ rejected ► agent
                 │                                                        └─ cancelled ► END
                 └─ read_only batch ─► tools ─► agent
check_output ─┬─ no defect ─► suggest_followups ─► write_semantic_cache ─► END
              ├─ defect, new reason ─► retry_output ─► agent
              └─ same reason twice ─► retry_exhausted ─► END
```

`agent` is the only node with a retry policy; `tools` recovers through
`handle_tool_errors=_friendly_tool_error`; every node is wrapped by `_instrumented` at
registration, never in its body.

## Complexity Tracking

> Filled because the Constitution Check found one deviation (D1), one verified bug (B1) and three
> advisories (A1, A2, A3). **Reconciled 2026-10-03:** B1 (#62) and A3 (#68, #69) are fixed and their rows moved to
> *Resolved since this spec was written* below; A2's worker catch-all was fixed in #64 and its row narrowed. G3 (cache key ignores conversation context) is a
> correctness gap rather than a constitution finding and is tracked in `research.md` / `tasks.md`.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **D1** — `compact_history` (may call the model) runs *before* `moderate_input`, so a blocked message on an over-ceiling conversation can still cost one summarization call. Literal text of Principle VI: "moderation MUST run before any retrieval or LLM spend." | Compaction must run on *every* valid turn regardless of what moderation decides about this turn's input, so history stays bounded even across rejected turns (`route_after_validation` docstring); and it summarizes *older* turns, never the new message, so it carries no injection exposure from the blocked text. Spend is bounded to one call per ceiling trip. | Reordering to `validate_input → moderate_input → compact_history` removes the deviation at near-zero cost (moderation reads only the last human message) and is recorded as an **unbuilt follow-up** in `tasks.md`. Not done retrospectively because it changes topology and would need `test_routing.py` and the quickstart updated in the same PR. Until then the constitution's literal wording is not met and is not claimed to be. |
| **A1** — no alert on `agent_context_retrieval_degraded_total` / `agent_moderation_ml_degraded_total`. | Neither leaves a human unaware of *committed business state*, which is Principle V's trigger for a mandatory alert; both are counted. | An alert on the moderation-classifier counter is cheap and arguably warranted (a security layer failing open silently). Left as a named follow-up rather than added here, because adding an alert rule changes `observability/prometheus/alerts.yml`, outside this docs-only change. |
| **A2** — (i) `ErrorCode.{MODERATION_BLOCKED,COST_CEILING_EXCEEDED,NO_PROGRESS,UNATTENDED_PAUSE}` registered but not emitted; (ii) `CHECKPOINT_LOST/INCOMPATIBLE` registered but a refused resume emits `{"type":"error","content":"checkpoint_lost: …"}` with no `code`; (iii) the first-event deadline emits an `error` event with no envelope (the worker catch-all, `content: str(exc)`, was fixed in #64). | (i) are *assistant answers* (refusal / fallback text + `done`), deliberately not error events. (ii)/(iii) are historical: those paths predate the envelope (pattern 30) and were not migrated. | (i) Emitting them as `error` events would change client behavior for every consumer (web UI, CLI, Telegram) and the SSE contract. (ii)/(iii) are a small, contained migration — wrap each in `ErrorEnvelope` (the catch-all as `INTERNAL` with a **generic** message, logging the real one) — but it changes an observable payload, so it is a follow-up task with a test, not a docs-batch edit. The raw-`str(exc)` path is the one worth doing first because it can surface internal text to a caller. |

### Resolved since this spec was written

| Id | What was wrong | Fixed in | Evidence |
|----|----------------|----------|----------|
| **B1** | `subagent_spend`'s per-turn reset was a no-op (append-only reducer); earlier turns' delegated spend counted against later turns' ceilings | #62 | reset-aware reducer `_concat_or_reset` in `app/agent/graph.py`; `tests/agent/test_safety_budgets.py::TestPerTurnResetThroughTheGraph`, `TestConcatOrResetReducer` |
| **A2 (worker catch-all)** | the stream core, the legacy stream and the worker forwarded an unexpected exception's own text to the caller | #64 | `internal_error_envelope` in `app/core/errors.py`; `tests/agent/test_streaming_terminal_events.py::TestAGraphFailureNeverLeaksItsMessageToTheCaller`; updated worker tests |
| **A3** | the cache's tag-escape guarantee was untested by name and the principal axis not at all; closing that exposed that `\|` and `\\` were unescaped (Principle I) | #68, #69 | `tests/retrieval/test_semantic_cache.py`, `test_semantic_cache_tag_escaping.py`, `tests/integration/test_semantic_cache_tag_escaping_real_redis.py` |
