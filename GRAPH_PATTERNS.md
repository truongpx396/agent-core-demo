# Enhanced LangGraph Patterns

## Why This Matters vs. Basic "LLM + Tools"

This graph shows **realistic patterns** you'll use in production LangGraph agents:

### 1. **Input Validation with a Real Exit (`validate_input` + `route_after_validation` + `reject_input`)**
Guard early and actually terminate the invalid path instead of falling through. Routing lives in a separate conditional-edge function (`route_after_validation`), which sends bad input to `reject_input` (a dedicated node returning an `AIMessage` — the *system* speaking, not the user) and ends at `END`; valid input proceeds to `retrieve_context`.
- **Real-world**: empty strings, format checks, permissions, rate-limiting — with an actual short-circuit.

### 2. **Context Enrichment (Node: `retrieve_context`)**
Pre-fetch and prepare context *before* the LLM reasons (RAG), reducing tool calls and improving accuracy. `agent` appends retrieved context as a `SystemMessage` right before invoking the LLM — the base `SYSTEM_PROMPT` is seeded once per thread (`app/agent/runtime.py::_ensure_seeded_async`), so `agent()` only adds per-turn context on top.

### 3. **State Tracking (State fields: `iterations`, `context`)**
Flow control lives explicitly in state, enabling loop limits, retries, and conditional routing.

### 4. **Conditional Routing (Edge fn: `should_continue`)**
An *edge function*, not a node — never appears in the graph's node list, just decides where execution goes after `agent`. Not every query needs tools, not every tool call succeeds.

### 5. **Output Quality Gate with a Real Retry (`check_output` + `route_after_check` + `retry_output`)**
Validate the final answer and act on it, not just pass through to `END`. `route_after_check` inspects the last message; if suspiciously short, routes to `retry_output`, which appends a corrective `HumanMessage` and loops back to `agent`. `MAX_ITERATIONS` still bounds total retries.

### 6. **Loop Control (via `MAX_ITERATIONS`)**
Shared safety net for both the tool-calling loop and the output-retry loop.

### 7. **Error Recovery (`ToolNode(TOOLS, handle_tool_errors=_friendly_tool_error)`)**
A tool exception becomes a `ToolMessage` the agent sees next turn, not an unhandled exception that kills the run. `handle_tool_errors` turns the exception into a short string so the agent can react instead of crashing the graph.
- **Three failure modes, three different policies**: a mid-turn tool failure recovers via the mechanism above; `retrieve_context` failing *degrades* to no pre-fetched context (enrichment, not required — the LLM still has `search_docs`); the `agent` node's own LLM call gets an automatic *retry* (`AGENT_RETRY_POLICY`) since a failed LLM call has nothing to fall back to. LangGraph's default `retry_on` already excludes programming errors, so retry can't mask a real bug.

### 8. **Human-in-the-Loop (`human_approval` + `route_after_approval`, gated by `require_approval` — and, since `add_note`, also mandatory)**
Pause the graph before running tool calls via LangGraph's `interrupt()`. `interrupt()` suspends `human_approval` and persists state via the checkpointer; a caller resumes with `graph.invoke(Command(resume=True/False), config)`. **Two independent routes reach it**: the opt-in `require_approval` flag, and — since pattern 15 — a **mandatory** route for any non-`read_only` tool call. See `app/channels/chat.py`'s `--hitl` mode (`make chat-hitl`) for a runnable example.
- **Gotcha**: a rejected approval still needs a matching `ToolMessage` per pending `tool_call`, or the next LLM call fails — `human_approval` synthesizes rejection `ToolMessage`s before routing back to `agent`.
- **Unattended callers**: not every caller can answer an approval prompt. `astream_events_turn_unattended` (pattern 43) auto-declines (`Command(resume=False)`) for callers with no human on the other end (`app/channels/telegram.py`, fire-and-forget queue jobs) — see `agent_unattended_pause_total`.
- **Real-world**: approval gates before sending email, writing to a DB, spending money, or any other side-effecting call.

### 9. **Parallel Tool Execution (built into `ToolNode`, no extra code)**
When one LLM turn requests multiple tool calls, `ToolNode` already runs them concurrently — no separate fan-out/fan-in node needed for "multiple tool calls in one AI turn" (LangGraph's `Send` API is for the different case of fanning one node over dynamic *graph* branches).

### 10. **Multi-Layer Safety Budgets**
`MAX_ITERATIONS` alone isn't a safety net, it's one layer. Each budget catches a failure mode the others can't:

| Layer | Where | Catches |
|-------|-------|---------|
| `MAX_ITERATIONS` | `should_continue` | Model stuck calling tools forever |
| `MAX_TOOL_CALLS_PER_TURN` | `should_continue` → `too_many_tool_calls` | One turn fanning into dozens of tool calls at once |
| `MAX_TOKENS_PER_TURN` | `should_continue`, tracked in `agent()` | A single turn burning unbounded spend even with few iterations |
| `HISTORY_TOKEN_CEILING`/`FLOOR` (`_trim_history`/`compact_history`) | Every turn, before moderation/retrieval | A long thread's `messages` list growing unbounded (pattern 13, 41) |
| `MAX_COST_USD_PER_TURN` | `should_continue`, `agent()` | Same token count, different $ across model tiers (pattern 35) |
| `MAX_REPEATED_ACTIONS` | `should_continue` | Model stuck retrying the SAME call with the SAME args (pattern 34) |
| `TOOL_TIMEOUT_SECONDS` | Worker-thread `.result(timeout=...)` per tool call | A hung Qdrant/embedding call, or a pathological input |
| `REQUEST_TIMEOUT_SECONDS` | Wraps the whole turn | Several slow-but-not-hung steps adding up |
| `recursion_limit=12` (`_config`) | LangGraph's own graph-step cap | A routing bug causing a node cycle |

- **Why layered**: each bounds a different thing (loop count, fan-out width, spend, one call's latency, the whole turn's latency, graph structure) — one check can't cover all of them.
- **Gotcha fixed**: `iterations`/`total_tokens` persist across the whole checkpointed thread unless reset — `validate_input` resets both to `0` every turn (but not on HITL resume).
- **Tool-call budget mechanics**: rejecting an over-large batch reuses the same `ToolMessage`-per-pending-call shape `human_approval` needs (`_reject_tool_calls`), then loops back to `agent`.

### 11. **Custom Metrics (`app/core/metrics.py`, `app/core/telemetry.py`, pushed via OTLP)**
OpenTelemetry counters/histograms for the aggregate view Langfuse's per-call tracing doesn't give ("how often, across everyone"). `app/core/metrics.py` wraps the real OTel API in a small prometheus_client-shaped surface (`.labels(...).inc()`), so no call site elsewhere had to change.
- **Push, not pull**: `configure_telemetry()` (called at real process startup, never at import time — see its own docstring) pushes to a shared otel-collector, giving ONE aggregated scrape target across the API and every independently-scaled worker replica — a pull-based `/metrics` on the API alone could never see a worker's metrics.
- **Two wiring mechanisms**: `MetricsCallbackHandler` (generic LangChain callback hooks) for tool-call counts/errors with zero node instrumentation; direct `.inc()` calls inside nodes that can fire more than once per turn or need to record a decision/degradation a generic callback can't distinguish.
- **Explicit histogram buckets** (`View`s on the `MeterProvider`), not OTel SDK defaults — verified the defaults (tuned for ms-scale web requests) put every real 1-90s turn in one bucket.
- **Real-world**: `docker-compose.observability.yml` (`make obs-up`) — Prometheus, Grafana, Loki, Alertmanager, wired up, not hypothetical.

### 12. **Untrusted Content Framing (`<retrieved_document>` delimiters + a `SYSTEM_PROMPT` rule)**
Retrieved context is wrapped in `<retrieved_document>` delimiters, and `SYSTEM_PROMPT` states once that delimited content is data, not instructions — a retrieved document is a textbook prompt-injection vector. The fix is structural: delimited text is never eligible to be read as an instruction in the first place, so the model doesn't have to notice an injection attempt.
- **Real-world**: any RAG pipeline or tool whose output could contain adversarial text — assume it eventually will, and frame accordingly from day one.

### 13. **Bounded Conversation History (`HISTORY_TOKEN_CEILING`/`FLOOR` / `_trim_history` / `_messages_to_trim`)**
`messages` is the only `State` field with no cap by construction. `compact_history` (right after `validate_input`) trims it via `RemoveMessage` using an estimated-token-count hysteresis check: no-op at/under `HISTORY_TOKEN_CEILING`; once exceeded, drops OLDEST whole turns until at/under the lower `HISTORY_TOKEN_FLOOR`.
- **Why hysteresis, not "trim back to N every time"**: trimming to the SAME ceiling every time re-triggers `compact_history`'s own summarization call on nearly every turn once past the threshold, and shifts every token after the cut, invalidating the provider's prompt-prefix cache. A materially lower floor buys several turns of cache-friendly growth before re-triggering.
- **Why a token estimate, not a turn count**: turns vary wildly in size; the token estimate (`tiktoken`, approximate — no bundled local tokenizer) tracks what actually matters, the model's context window.
- **Turn-aware, not a raw slice**: a turn is `HumanMessage` through the next `HumanMessage`, so a `tool_call`/`ToolMessage` pair is never split (an orphaned `tool_call` fails the next LLM call, same as the HITL-rejection gotcha in pattern 8). The seeded system prompt and the most recent turn are never dropped.
- **No longer just discarded**: dropped turns are folded into a cumulative summary — see pattern 41.
- **Real-world**: any agent whose conversations can run long.

### 14. **Node Telemetry (`_instrumented`, structured lifecycle logs)**
Every node is wrapped, at graph-registration time in `build_graph` (never inside the node itself), by a decorator that logs `node_started` then `node_completed`/`node_failed`/`node_paused`, carrying the node name, a per-turn `run_id`, and (for terminal records) `duration_ms`.
- **Why**: Langfuse answers "what happened in this run," OTel metrics (pattern 11) answer "how often across everyone" — neither gives a grep-able trail or tells you a node is hung right now. `human_approval`'s `interrupt()` (a `GraphInterrupt`) is a normal pause, not a failure — logged as `node_paused` and re-raised untouched.
- **Never logged**: message content or the `state` dict — metadata only, so this can't become a second, unscrubbed copy of prompt/document text outside Langfuse.
- **structlog under the hood**: `configure_logging` wires structlog onto the stdlib root logger — every existing `logger.info(...)` call is unchanged.
- **Real-world**: Promtail ships every container's JSON logs to Loki (`make obs-up`), queryable in Grafana; `run_id` is the join key across a request's logs and metrics.

### 15. **Tool Capability Declarations (`app/agent/tools.py::TOOL_CAPABILITIES`) — a Mandatory Gate, Not Just an Opt-In One**
Every tool declares `read_only`, `mutating`, or `outward` in `TOOL_CAPABILITIES`. `should_continue` checks it on every pending batch: any non-`read_only` call routes through `human_approval` **unconditionally**, regardless of `require_approval`. A tool missing from the mapping defaults to `outward` — fail closed.
- **Why mandatory, not opt-in**: a RAG agent already carries untrusted-content exposure on nearly every turn (pattern 12); adding write capability on top is one gamble away from an untrusted document steering a real write. There is no flag that turns this off.
- **`add_note` is the one `mutating` tool** and a worked example of a companion principle: a write tool is a fixed, typed, closed-vocabulary operation, never a query the model constructs — its point id is always a fresh UUID, never caller-supplied, so it can only append, never target/overwrite by guessing an id.
- **Real-world**: any agent with a tool that sends, spends, deletes, or writes.

### 16. **Durable Checkpointing + Cross-Restart Compatibility (`init_graph_async`, `STATE_SCHEMA_VERSION`, `resumability_error_async`)**
`build_graph()` defaults to `MemorySaver` for tests; the shared runtime singleton uses `AsyncPostgresSaver` (own database in the stack's Postgres) so a paused `human_approval` survives a restart/redeploy — and so several processes (API + worker replicas) can share the same checkpoint store, which a single SQLite file's writer-locking can't do safely.
- **Why every graph caller is async**: `AsyncPostgresSaver`'s lock is bound to whichever event loop created it; `init_graph_async()` opens it on the calling loop, so every process must await it on that same loop.
- **`STATE_SCHEMA_VERSION` + `graph_version`**: every turn stamps the schema version (bumped only on a breaking `State`/topology change) and a build id into state, so a checkpoint records which build wrote it.
- **`resumability_error_async`**: called before every resume. `checkpoint_lost` (no paused run) vs. `checkpoint_incompatible` (schema mismatch) — a differing build SHA alone is NOT an error, since ordinary deploys change it constantly.
- **Real-world**: any agent where a paused HITL gate needs to actually survive a deploy.

### 17. **Multi-Tenant Isolation (`SecurityCtx` + `Policy`, enforced as a Qdrant pre-filter)**
A `SecurityCtx` (`tenant`, `principal`, `claims`) is stamped once by `validate_input` from `config["configurable"]["ctx"]` — never from message content. `Policy.permit()` decides if an action may happen at all; `Policy.lower(ctx, target)` turns ctx into a Qdrant `Filter` applied **inside the query**, never as a Python post-filter.
- **Why pre-filter, never post-filter**: a buggy post-filter and a correct one look identical until a cross-tenant leak shows up on an untested query. A store-native predicate fails loudly instead.
- **Two isolation axes**: `documents` scopes to `tenant`; `memories` additionally scopes to `owner` — both share one collection, distinguished by a `kind` field that's itself part of the filter.
- **Fail closed, checked first**: `route_after_validation` checks `valid_ctx` before the empty-input check; every ctx-aware tool repeats the check independently (defense in depth, since a tool call is a different code path).
- **Not authentication**: `get_ctx` reads trusted headers (`X-Tenant-Id`/`X-Principal-Id`) — nothing verifies a password or JWT. A real auth gateway setting these headers is a deployment change, not a rewrite.
- **Real-world**: any agent serving more than one customer/workspace against a shared store.

### 18. **Cross-Session Memory (`remember` + automatic recall, re-filtered every read)**
`remember` is the only way a memory gets written — declared `mutating`, gated like `add_note`. Recall is automatic (folded into retrieval), on the model's initiative to *write* but never its initiative to *read*.
- **Why writing is opt-in, reading isn't**: whatever writes memory decides what gets replayed into every future prompt — an autonomous write would be a privileged side channel. Nothing here extracts facts from turn text on its own.
- **Re-filtered every call**: `recall_memories` applies `Policy.lower` fresh each time, scoped to current tenant AND owner — never cached.
- **Framed like a retrieved document, but more so**: a poisoned document affects one answer; a poisoned memory replays on every later turn until removed.
- **Deliberately not built**: an LLM-facing delete/forget tool — `delete_by_filter` is a support function for a real data-subject-request script, not a model-invokable tool.
- **Real-world**: any agent that should remember a stated preference across sessions.

### 19. **Prompt-Cache Stability (`SYSTEM_PROMPT` stays ctx-free by construction)**
`SYSTEM_PROMPT` is a plain constant — no principal, tenant, or timestamp ever interpolated in. `SecurityCtx` flows through `config`/`state["ctx"]` only, never into the message list sent to the LLM.
- **Why this catches the classic cache-buster**: a prefix embedding `ctx["principal"]` looks perfectly stable *within one conversation* — the bug only shows up once a second principal's traffic shares the manifest and gets a differently-priced prefix. `tests/agent/test_prompt_cache_stability.py` asserts byte-identical rendering across two different ctx values, plus an independent leak sweep.
- **Real-world**: any agent behind a provider that discounts a stable prompt prefix (Anthropic, OpenAI) — the discount silently disappears the moment something request-specific sneaks into the cached region.

### 20. **Hybrid Retrieval + Cross-Encoder Rerank + Cited Answers (`qdrant_store.py::hybrid_search`, `tools.py::gather_context`, `check_output`)**
`search_docs`/`recall_memories`/`retrieve_context` run `hybrid_search()`: two parallel Qdrant legs (dense + BM25 sparse via `fastembed`), fused server-side (`FusionQuery(fusion=Fusion.RRF)`), then reranked by a local cross-encoder. Every hit is numbered (`[1]`, `[2]`, ...); `check_output` scans the final answer for the markers it actually contains (`_used_citations`) and writes only those to state — **never trusting the model's own claim about what it cited**.
- **Two independent degradation layers**: sparse-leg failure falls back to dense-only (`agent_retrieval_degraded_total{stage="sparse"}`); reranker failure returns the RRF-fused order as-is (`stage="rerank"`). Either way the answer is still fully cited.
- **Why local ONNX models, not an API call**: no network round trip on the retrieval hot path, no extra external dependency.
- **Why citations are computed, not asked for**: `_used_citations` regexes the actual answer text and intersects with what retrieval actually offered — a hallucinated or forgotten marker both resolve correctly without trusting model self-report.
- **Real-world**: any RAG system needing a real audit trail, not "the model said it used source 3."

### 21. **Fixed-Tool Structured Data Access + MCP Exposure (`sql_store.py`, `tools.py::query_employees`, `mcp/server.py`)**
`query_employees` is a closed, typed query over Postgres — `tenant` (from `SecurityCtx`), `department` (a closed enum), `name_contains` are the only variables; no `execute(sql: str)` escape hatch anywhere. Every query is parameterized and always ANDs `WHERE tenant = %s` — filters can only narrow the tenant scope, never widen it.
- **Same principle as pattern 15's `add_note`, for reads**: a tool is a fixed, typed, closed-vocabulary operation, never a query the model constructs — the access boundary lives in reviewable code, not a string the model writes.
- **MCP exposure is a SEPARATE trust boundary**: `mcp/server.py` wraps the same fixed query behind `FastMCP` for external clients — but MCP has no equivalent of `RunnableConfig`, so `tenant`/`principal` are explicit tool arguments here, checked against the same fail-closed `Policy.permit` gate. It does NOT authenticate the MCP caller — a production server would derive identity from the client's own verified auth.
- **Real-world**: HR/directory lookups, order status, inventory counts — where a text-to-SQL tool would be tempting and exactly wrong.

### 22. **Semantic Cache (`app/retrieval/semantic_cache.py`, `check_semantic_cache` + `write_semantic_cache` nodes)**
`check_semantic_cache` (right after validation, before retrieval) embeds the query and does a cosine-KNN lookup in Redis Stack, scoped to tenant **and** principal. A hit within `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` (0.95) short-circuits straight to a final `AIMessage` — no retrieval, no LLM call — then rejoins at `check_output`. `write_semantic_cache` writes back on a confirmed-final, non-retry turn, unless the turn was itself a cache hit.
- **Why tenant AND principal**: a cached answer can carry citations into a principal's own memories — the cache must be at least as narrow as the memory filter.
- **A latency optimization, never a correctness dependency**: `get`/`set` catch every exception and degrade (miss / no-op write), recording `agent_semantic_cache_total{outcome=...}`.
- **Gotcha**: RediSearch TAG queries treat `-` as query syntax — an unescaped tenant value like `"other-co"` raised a syntax error; fixed via `_escape_tag`.
- **Real-world**: FAQ-style support, internal docs Q&A — repeated near-duplicate questions.

### 23. **Config-First Multi-Domain Composition (`AgentManifest` + `DomainPlugin`, `build_graph(manifest=..., domain=...)`)**
`build_graph()` — unmodified topology — adapts to a new domain by swapping an `AgentManifest` (config: name, system prompt, exposed tools) and a `DomainPlugin` (code: tool implementations, capabilities, policy). `DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN` wrap the existing Ecorp setup unchanged. There is no `if domain == "..."` anywhere in `graph.py`.
- **"Port-swapping," not just parameterization**: `should_continue` needs a domain's own capability mapping but LangGraph calls it with only `state` — solved via `functools.partial(should_continue, tool_capabilities=...)`, bound once per `build_graph()` call, with a default parameter so every existing direct caller is unaffected.
- **The load-bearing test**: `tests/agent/test_manifest.py` builds a second domain with a genuinely different `Policy` class and one Ecorp-unknown tool, then proves the unmodified mandatory-approval gate correctly treats it as `mutating` using THAT domain's own capability map — plus a structural check that Ecorp's tools are genuinely absent from this domain's `ToolNode`.
- **Circular-import gotcha**: `manifest.py` needs `graph.py`'s `SYSTEM_PROMPT`; `graph.py`'s `build_graph()` needs `manifest.py`'s defaults — resolved via a deferred import inside `build_graph()`'s body.
- **What this doesn't do**: serve multiple domains from one running process — that's a further increment (pattern 47's own note).
- **Real-world**: one hardened runtime (safety budgets, HITL, telemetry, checkpointing) shared across several products/deployments — see `app/domains/` (pattern 47).

### 24. **General-Purpose Ingestor with Parent-Child Chunking (`app/ingestion/chunking.py`, `app/ingestion/ingestor.py`)**
`ingest_text`/`ingest_file`/`ingest_url` funnel through the same chunk → embed → `build_point` pipeline `make ingest` now uses too (one pipeline, not two that can drift). Every item is stamped with the owning tenant and refused without one.
- **Parent AND child chunks**: a single chunk size fights two jobs — retrieval wants small/precise, answer quality wants surrounding context. `chunk_text` splits into ~1200-char **parent** chunks, then ~600-char overlapping **child** chunks; only the child is embedded, but the payload carries `parent_text`, which the agent prefers when present.
- **Why overlapping**: a hard boundary can cut the one sentence that answers a query in half; the sliding window ensures at least one child captures it whole.
- **Dedup**: several child chunks from the same parent can all score highly — `_dedupe_by_parent` keeps the highest-ranked hit per `parent_id`.
- **SSRF guard**: `_assert_safe_url` requires `https://`, resolves the FULL A/AAAA record set and rejects if any address is private/loopback/reserved, and disables redirects. Disclosed gap: this validates-then-fetches, so a narrow DNS-rebinding race isn't fully closed.
- **Real-world**: a standalone product ("point it at a folder/URL/text"), not a demo answering only over pre-loaded content.

### 25. **Input Moderation Before Any Spend (`app/agent/moderation.py`, `moderate_input` + `route_after_moderation` + `reject_moderation`)**
Runs right after `validate_input`'s ctx/empty checks, before the semantic cache, retrieval, or any LLM call. `moderation.screen(text)` checks known injection/jailbreak phrasings plus a small denylist; a match short-circuits to `reject_moderation` → `END`.
- **A real check, not a no-op default**: pattern-based, not an ML classifier (which would mean a hosted API or a second local model on the hot path) — honestly scoped, testable behavior against known patterns, not a claim of understanding intent.
- **Fail-open only on the check's OWN failure**: a genuine match fails closed and blocks the turn; an exception in the moderation code itself fails open, recorded as `outcome="error"` — the two are deliberately not conflated.
- **Real-world**: any agent with a public/semi-trusted input surface.

### 26. **Real Usage/Cost Ledger (`app/agent/usage_ledger.py`, wired into `runtime_stream.py::_record_turn_metrics`)**
`record_usage(ctx, thread_id, model_alias, total_tokens)` writes one row per completed turn into `usage_ledger` (same `appdata` Postgres), tenant+principal scoped. Wired at the one "a turn actually completed" call site.
- **Why a real ledger, not a discarded counter**: `MAX_TOKENS_PER_TURN` bounds spend per turn but keeps no durable record of what was actually spent, by whom — `usage_summary` is the read path proving it isn't write-only.
- **Cost is approximate, honestly scoped**: `PRICE_PER_1K_TOKENS_USD` is $0 for any unlisted alias (every locally-run Ollama model); pointing at a real paid provider is a config change.
- **Real-world**: any agent serving more than one team/customer where "how much did this cost, by whom" needs to be answerable.

### 27. **Clarification and Follow-Ups Without Forking the Graph (`ask_clarification` tool, `suggest_followups` node)**
Two different needs, two different mechanisms. Clarifying is `ask_clarification` — an ORDINARY `read_only` tool, no new node/routing: its result becomes a `ToolMessage`, and `SYSTEM_PROMPT` instructs the model to relay it verbatim next turn. Follow-ups are a real node, `suggest_followups`, after `check_output`'s non-retry branch, generating 2-3 short questions via one more small LLM call.
- **Why follow-ups gate on `used_citations`**: an answer with nothing derived (a refusal, a clarification, general knowledge) naturally has empty `used_citations` — one existing signal suppresses follow-ups for all three cases.
- **Why `suggest_followups` is skipped on a cache hit**: a semantic-cache hit means zero LLM calls for the turn; an unconditional follow-up call would silently reintroduce one, breaking a property the test suite asserts.
- **Real-world**: `ask_clarification` for agents where a wrong guess costs more than one extra turn; `suggest_followups` for consumer chat surfaces where "what next" chips help engagement.

### 28. **Consuming a Remote MCP Tool Catalog (`app/mcp/client.py::load_remote_tools`)**
The reverse of pattern 21's server direction: `load_remote_tools(command, args, capability_overrides)` connects to an external MCP server over stdio, lists its tools, and wraps each as a LangChain `StructuredTool` with the remote's own JSON Schema passed straight through.
- **Why `capability_overrides` is the ONLY source of truth**: MCP's `ToolAnnotations` are self-reported hints, not verified guarantees (confirmed: this app's own server sets none). A remote tool not explicitly named defaults to `"outward"` — the same fail-closed default as an undeclared in-process tool. Once merged into a `DomainPlugin`, the mandatory gate applies with no special-casing.
- **Sync AND async by necessity**: a sync `func` (bridging via `asyncio.run()` for `graph.invoke()`) and an async `coroutine` (for `astream_events`, already inside a running loop). One fresh connection per call — simpler than a persistent session, at the cost of per-call latency, a disclosed tradeoff.
- **Real-world**: reaching tools this app doesn't own the implementation of, without inheriting whatever the remote claims about its own safety.

### 29. **Built-In Web UI (`app/api/static/index.html`, `GET /` in `app/api/main.py`)**
A single self-contained HTML file (inline CSS/JS, no build step, no CDN) served at `GET /`, talking only to `POST /chat/stream/queued` and rendering exactly the published SSE vocabulary (`token`, `tool_start`, `tool_end`, `citations`, `approval_required`, `error`, `done`) — no bespoke endpoint of its own.
- **Why `fetch()`, not `EventSource`**: the queued endpoint is POST and needs `X-Tenant-Id`/`X-Principal-Id` headers, which `EventSource` can't send — so the page manually parses SSE frames from a `fetch()` stream.
- **A real bug this surfaced**: manually exercising this page against a live `uvicorn` process caught `asyncio.InvalidStateError` from sync checkpointer calls (`update_state`/`get_state`) running on the same loop the async checkpointer was opened on — three call sites needed their async counterparts (`aupdate_state`/`aget_state`, a new `resumability_error_async`), a defect the `MemorySaver`-backed test suite structurally couldn't catch.
- **Real-world**: a usable interface without a frontend framework or build pipeline — appropriate for a demo/reference app.

### 30. **Canonical Error Envelope (`app/core/errors.py`'s `ErrorCode`/`ErrorEnvelope`)**
One shape — `{code, message, details}` — for every SSE `error` event and CLI error, drawn from a single `ErrorCode` enum rather than each call site inventing its own string.
- **Why NOT applied to `ToolMessage` content**: what a failing tool returns to the LLM is natural-language by design, for a different audience (the model). Wrapping it would just be JSON the model has to re-parse into prose.
- **Real-world**: any surface with more than one caller that needs to distinguish "retry this" from "don't" programmatically.

### 31. **DB Connection Pool (`app/agent/sql_store.py`'s `ConnectionPool`)**
A single `psycopg_pool.ConnectionPool` singleton (lazy, `min_size=1`/`max_size=10`) behind `get_connection()` — every call site is unchanged, so pooling is a transparent internal change. `close_pool()` is wired into the API lifespan shutdown (without it, background worker threads outlived process shutdown, a real reproduced bug).
- **Real-world**: any app issuing more than a handful of DB queries/sec.

### 32. **Credential/Secret Scrubbing on Tool Output (`app/core/scrubbing.py`)**
Two layers at the one chokepoint every tool result funnels through: (1) static regex patterns for common credential shapes (API keys, AWS ids, `password=`/`token=` pairs, embedded `user:password@`, JWTs), and (2) this deployment's own bound secret values read from config, so an exact echo of a real configured secret is caught too.
- **Why tool output specifically**: it never passes through the model's own input — a raw DB row or API response can carry a credential nobody asked for straight into `ToolMessage.content`, which a prompt-level scrubber would never see.
- **Fails open**: degrades to unscrubbed text (logged) on its own internal failure.
- **Real-world**: any agent whose tools touch systems that can legitimately contain secrets in their data.

### 33. **Memory Deletion Audit + Age Selector + Retention-at-Recall (`app/agent/memory.py::delete_memories`, `Policy.lower`)**
`delete_memories(ctx, *, memory_id=None, older_than_days=None, target_principal=None)` requires EXACTLY ONE selector, refusing an ambiguous request rather than silently narrowing to "everything." Counts matches before deleting with the same filter, so the return value is accurate. Every call, refused or not, is recorded via metrics + a structured log.
- **Retention-at-recall, independently**: `Policy.lower`'s memory filter ANDs a `DatetimeRange(gte=cutoff)` (`MEMORY_RETENTION_DAYS`, default 365) onto every recall, not just a manual sweep — an unswept expired memory is invisible at read time regardless.
- **Real-world**: GDPR-style "right to erasure" or a support tool's "delete my data" button.

### 34. **No-Progress Detection (`_consecutive_repeat_count`, `_tool_call_fingerprint`, checked in `should_continue`)**
A per-turn check (scans backward only from the most recent `HumanMessage`). `_tool_call_fingerprint` normalizes a batch (tool name + sorted args) order-independently; `MAX_REPEATED_ACTIONS` (3) identical consecutive batches ends the turn — independently of, and usually before, `MAX_ITERATIONS`.
- **Why a pure function of `state["messages"]`**: the message history already IS the record of what's been tried — no second, driftable counter needed.
- **Real-world**: a model stuck retrying an identical failing call verbatim — a real, observed failure mode.

### 35. **Cost Ceiling Enforcement (`MAX_COST_USD_PER_TURN`, checked in `should_continue`, computed in `agent()`)**
A hard stop on cumulative per-turn dollar cost, distinct from `MAX_TOKENS_PER_TURN` since the same token count costs differently across model tiers. `agent()` computes incremental cost using the same price table the usage ledger (pattern 26) uses — one source of truth. Checked BEFORE the next call, not just after — enforcement, not just recording.
- **Real-world**: a tool loop that could run away against a metered, real-dollar-cost model API.

### 36. **Run Cancellation (`CANCEL_SENTINEL`, third `human_approval` outcome, `runtime_stream.py::cancel_run`)**
A genuinely third outcome for a paused interrupt, not a repurposed rejection. `route_after_approval` checks `cancelled` FIRST and routes straight to `__end__`, never back to `agent`.
- **Why a real third branch**: a rejection still lets the agent react; a cancellation must guarantee the run stops, unconditionally.
- **Real-world**: a human changing their mind mid-review ("actually, just stop"), distinct from disapproving one action.

### 37. **Structured Per-Tool-Call Audit Record (`MetricsCallbackHandler`, `app/core/metrics.py`)**
LangChain's own `run_id` correlates a `tool_called` start log with its matching success/failure log. Args and results are logged as a SHA-256-truncated fingerprint, never raw content — extending pattern 14's "never message content in a generic log" rule to tool-level audit logging.
- **Real-world**: "which tool ran, with what, and did it succeed" answerable from grep-able logs alone at 3am.

### 38. **Resolved Concrete Model Recording (`app/agent/model_resolver.py::resolve_model`, wired into `usage_ledger.py::record_usage`)**
Queries LiteLLM's `GET /model/info` to resolve a routing alias (`"chat"`) to the concrete model actually serving it — neither the response body nor LangChain's own metadata carries this; only a proxy-specific response header does, which LangChain doesn't surface. Cached per-process; degrades to `None` on failure; recorded as `resolved_model` in the usage ledger.
- **Why this matters despite alias-only-by-design**: the single input with the largest effect on output quality can silently change (a gateway remap) with no other trace of it — this makes model choice invisible to routing but visible to forensics.
- **Real-world**: debugging a quality regression that turns out to be an unannounced backend model swap.

### 39. **Ungrounded Claims Count (`_ungrounded_claims_count`, computed in `check_output`)**
The mirror image of `_used_citations` (pattern 20), computed INDEPENDENTLY from the same answer text so a bug in one can't mask a bug in the other. Counts `[n]` markers that don't match any real citation actually returned.
- **Real-world**: making "did the model actually cite something real" a measurable, gateable property (pattern 40) rather than a spot-checked impression.

### 40. **Eval Statistical Rigor (`scripts/eval.py`'s `EVAL_REPETITIONS`/`REPETITION_PASS_THRESHOLD`/`GROUNDED_CLAIMS_THRESHOLD`)**
Two release gates, not one pass/fail per case. (1) Each case runs `EVAL_REPETITIONS` times (5); a case passes only if `REPETITION_PASS_THRESHOLD` (80%, 4-of-5) of repetitions individually passed — a stochastic system graded once is a coin flip. (2) A grounded-claims gate over the WHOLE golden set (≥95% of every citation marker must be real), computed from the runtime's own `used_citations`/`ungrounded_claims_count` machinery, never a model's opinion of itself.
- **Summed, not averaged**: tokens/cost/grounding counts are summed across a case's repetitions ("what did fully evaluating this cost"); latency is averaged ("how long does one attempt take").
- **Real-world**: the 80%/95% thresholds are demo defaults, but the STRUCTURE generalizes to any stochastic-system eval.

### 41. **Bounded History Summarization (`compact_history` node, `history_summary` State field, `route_after_compaction` + `context_window_exceeded`)**
Extends pattern 13's discard-only trim with an LLM-backed summarization step. `compact_history` summarizes exactly the discarded messages via one LLM call, folding the result into a CUMULATIVE `state["history_summary"]` that's never reset per-turn. `agent()` front-loads the bulk summary at the same fixed anchor retrieved context uses, while a short "don't restate this verbatim" reminder stays at the current tail (recency-weighted, since a small model was observed regurgitating the injected summary).
- **Why a separate field, not reordering `messages`**: LangGraph's `add_messages` reducer has no "insert at position N" primitive — every new message is appended.
- **A named terminal state**: if `history_summary` itself grows past `MAX_HISTORY_SUMMARY_CHARS`, `route_after_compaction` routes to `context_window_exceeded` — a real dead end, not silent truncation.
- **Degrades safely**: a summarization LLM failure still applies the trim; it just skips updating the summary.
- **Real-world**: a support thread referencing something discussed 20 turns ago, where dropping it outright would lose real information.

### 42. **Telegram Channel (`app/channels/telegram.py`)**
A fourth first-party interface — genuinely thin: long-polls `getUpdates`, resolves a stable `thread_id`/`SecurityCtx` per chat/user, and drives `astream_events_turn_unattended()`, collecting streamed output into one final reply.
- **Long-polling, not a webhook**: needs no inbound port/public URL, the right default for a local/demo deployment; a real production deployment would switch to `setWebhook`.
- **The one surface reaching the public internet**: everything else in this stack runs fully local. `TELEGRAM_BOT_TOKEN` is empty by default and `run()` refuses to start without one.
- **HITL reuses the existing auto-decline**: no interactive approve/reject UX here — a mutating/outward call gets auto-declined, with a real reply explaining why, never a silent write.
- **At-least-once delivery**: `offset` only advances after a reply attempt completes, so a message is retried (not replayed after success) if the process dies mid-handling.
- **Real-world**: any agent whose users already live in a chat app.

### 43. **Redis Streams as a Persistent Queue Between the SSE Service and Agent Workers (`app/job_queue/queue.py`, `app/job_queue/agent_worker.py`, `POST /chat/stream/queued`)**
Now the ONLY HTTP chat path this app serves. The producer (`app/api/main.py`) publishes a turn request onto one shared stream (`agent:requests`), then reads events back from a fresh per-request results stream and forwards them as SSE — it never runs the graph. The consumer (`agent_worker.py`, `make agent-worker`) pulls requests via a Redis Streams consumer group, guaranteeing each request reaches exactly one worker — running N workers is the whole scaling story.
- **Independently scalable**: the SSE tier scales concurrent connections; the worker tier scales concurrent turns — neither number constrains the other, unlike a single in-process `/chat/stream`.
- **Per-request results stream**: avoids one turn's events leaking into another's SSE response, with self-cleaning TTL (300s) plus explicit `DEL` on a normal finish.
- **At-least-once, not exactly-once**: a request is only ACKed after `process_request` finishes; a crashed worker leaves it pending. `XCLAIM`/`XAUTOCLAIM` redelivery is NOT wired up — a disclosed gap, not assumed away.
- **A real bug this surfaced**: `redis-py`'s async client defaults `socket_timeout` to 5s, racing directly against `XREAD`'s own server-side `BLOCK` window — every blocking read raised `TimeoutError` before Redis's own block ever elapsed. Fixed with `socket_timeout=None`, guarded by a regression test.
- **Real-world**: many idle browser tabs (cheap to hold) against a small, expensive GPU-backed worker pool (expensive to hold) — scaling them on the same axis wastes whichever is cheaper.

### 44. **Multimodal / Image-Question Support (`_build_human_content`, `_human_text`/`_human_has_content`, `images` on every entry point)**
`_build_human_content(text, images)` builds a plain string (byte-identical to before) with no image, or an OpenAI/LiteLLM multimodal content list when at least one is attached. This app never fetches/decodes an image itself — the backing model does.
- **Why a text-extraction helper was needed at five call sites**: `route_after_validation`, `moderate_input`, `check_semantic_cache`, `retrieve_context`, `write_semantic_cache` all read `.content` assuming a plain string, which a multimodal message's list-shaped content would break. `_human_has_content` additionally treats "at least one image part" as real content, so an image-only question isn't wrongly rejected as empty.
- **Two disclosed scope boundaries**: moderation screens words, not pixels (an image-only message passes unconditionally); retrieval/caching stay text-only (an image-only question searches/caches against an empty string).

### 45. **Skill Packages with Progressive Disclosure (`app/agent/skills.py`, `skill_search`/`use_skill`, `scripts/index_skills.py`)**
A two-tool pair: a bundled catalog of `SKILL.md` packages (YAML frontmatter + markdown body, one dir per skill under `skills/`), discovered by MEANING rather than binding every skill's full instructions permanently. `skill_search` hybrid-searches a small collection of `{name, description}`; `use_skill(name)` loads exactly one matched skill's full body — a turn that never needs a skill only pays for two tool schemas, not the total size of every skill.
- **Reuses `hybrid_search`**, not a fork — `ensure_collection`/`upsert`/`hybrid_search` gained an optional `collection` param, zero duplicated fusion logic.
- **Disk is content truth; Qdrant is only the search index** — `use_skill` reads the body from an on-disk registry, never from Qdrant, so there's no "index says X, file says Y" drift possible.
- **Deliberate scope boundary**: skills are a bundled, shared catalog (no `SecurityCtx` needed, like `calculator`) — a tenant-authored skill catalog is a real, larger extension not attempted here.
- **Real-world**: middle ground between "bind every capability's full instructions on every turn" and "hand-pick a fixed subset per deployment" — a searchable catalog that can grow to hundreds of skills without growing what's bound to any single turn.

### 46. **Subagents: Scoped, Isolated Delegation (`app/agent/subagents.py`, `run_subagent`)**
A genuinely separate nested agent run — a fresh `build_graph()` invocation with its own messages/prompt/budget, not more instructions loaded into the same context (that's what skills do). `run_subagent(subagent_name, task)` is one tool with a closed enum built from `AGENT.md` files under `subagents/<name>/`.
- **The load-bearing decision: subagents may ONLY use `read_only` tools**, enforced structurally at catalog-build time — anything non-`read_only` is dropped with a warning. So `run_subagent` itself is a plain, static `read_only` declaration, needing zero special-casing in the mandatory-approval gate.
- **Why not let subagents write, gated at the spawn boundary**: a subagent's nested graph runs synchronously on a throwaway `MemorySaver` inside one outer tool-call frame — there's no mechanism for a human to resume that specific nested run hours later, so a write-capable subagent would mean either an unresumable mandatory gate (a real deadlock) or a bypass of pattern 15's "no flag turns this off" invariant. The read-only restriction sidesteps the question entirely.
- **Recursion blocked structurally**: `run_subagent` is unconditionally stripped from every subagent's own resolved tool set, regardless of its `AGENT.md`.
- **Isolation**: a fresh `messages` list (the subagent's own system prompt entirely replacing `SYSTEM_PROMPT`, plus the delegated task as its sole `HumanMessage`); `SecurityCtx` inherited unconditionally; its own smaller fixed budget (`MAX_SUBAGENT_ITERATIONS=6`, `MAX_SUBAGENT_TOKENS_PER_RUN=4000`, its own cost ceiling and timeout) via the same `functools.partial` mechanism pattern 23 established; an ephemeral `MemorySaver`, never the durable saver.
- **Compiled once, reused**: a graph cached by `(domain, subagent_name)`, the same "compile once, invoke many" shape as the top-level singleton. Nested spend folds into the parent's own budget via a `subagent_spend` reducer field (needed because parallel tool calls could otherwise race a read-then-overwrite). Nested activity now traces/streams: parent callbacks + metadata thread into the nested `.invoke()`, and the client's token stream is guarded against leaking the subagent's own reasoning tokens.
- **A dedicated, leaner graph** (`build_subagent_graph()`) shares `build_graph()`'s own node functions via a factored `_assemble_shared_graph_parts()`, dropping 5 of 21 nodes each for a specific, provably-safe reason (semantic cache, follow-ups, and history-compaction nodes are all either cross-talk risks or mathematically unreachable given the smaller token budget).
- **Disclosed gap**: a new subagent needs a process restart to appear (eager, compile-time enum).
- **Real-world**: a multi-step lookup whose intermediate noise shouldn't pollute the main thread, or a task better matched by a specialist prompt — comparable to Claude Code's Task tool.

### 47. **Example Domains as a Load-Bearing Proof (`app/domains/`) — and Why a Cron Job Can't Call an Outward Tool**
`app/domains/support/`, `app/domains/ops/`, `app/domains/sales/` turn pattern 23's test-only proof into three real, runnable products on the SAME unmodified graph: a Tier-1 support copilot, an internal ops bot (reusing this repo's own Prometheus/alert thresholds), and a sales/CRM concierge — each with its own `store.py`/`tools.py`/`domain.py` following `sql_store.py`/`tools.py`'s exact conventions, plus a new shared `app/domains/policy.py::ActionAllowlistPolicy`.
- **The runtime change, kept minimal**: `init_graph_async` gained an optional `manifest`/`domain` pair (defaulting to `None`, reproducing today's exact behavior) — deliberately NOT the "several domains from one process" registry (still unbuilt), just "which ONE domain does THIS process boot as," decided once at start from `AGENT_DOMAIN`.
- **The gate this exposed: an unattended cron job can never approve itself.** `post_to_team_channel` is the first real use of `outward` — and the mandatory gate has no bypass flag. Auto-declining would silently make a digest never post. Both cron scripts (`scripts/ops_digest.py`, `scripts/followup_sweep.py`) sidestep this by calling the domain's own `_impl` functions directly — a fixed pipeline, never entering the tool-calling loop for the write step.
- **Real-world**: a platform team's answer to "we need a support bot, an ops bot, and a sales bot" — one hardened graph underneath three different products.

### 48. **Real-Backend Testing at Every Layer: Testcontainers, a Shared Ollama, Playwright, promptfoo, garak, deepeval**
`tests/containers.py` generalizes the durable-checkpoint test's "skip cleanly if unreachable" contract into four `ensure_*()` helpers (Postgres, Redis, Qdrant, Ollama), each starting its own ephemeral testcontainer — so `tests/integration/`/`tests/live/` genuinely execute in CI while a laptop `pytest -q` with no Docker stays exactly as fast as before. `promptfoo/` and `garak/` are separate tools pointed at the same real small Ollama model directly.
- **Shared-container caching under pytest-xdist needed a fixed cache dir, not pytest's own rotating `basetemp`** — the textbook `tmp_path_factory.getbasetemp().parent` recipe is subtly wrong for both a plain non-distributed run (it overshoots into a directory reused across every invocation) and the controller process (which never calls `getbasetemp()` itself, so it can't be trusted for teardown). Ryuk is disabled and `pytest_sessionfinish` does teardown explicitly, gated on being the controller/non-distributed run.
- **A real concurrency bug this caught**: several xdist workers concurrently calling `init_graph_async()` for the first time against one shared Postgres hit `UniqueViolation` in `AsyncPostgresSaver.setup()`'s own migration bookkeeping (a real gap in the upstream library) — sidestepped by running `.setup()` once, inside its own lock, before any test can race it.
- **Real native tool-calling confirmed, not assumed**: `OPENAI_API_BASE` points straight at Ollama's own OpenAI-compatible endpoint, verified via a raw `curl` round trip to have real structured `tool_calls`, no prompt-injection faking (unlike LiteLLM's own `ollama/` provider).
- **promptfoo/garak config-schema gotchas, found by running them, not trusting docs**: promptfoo's `file://` paths resolve relative to the config file's directory, not the CWD; garak's `generators.openai...uri` must nest under a top-level `plugins:` key or it's silently ignored, falling back to a hardcoded default.
- **Small local models are unreliable graders AND targets**: a garak `dan` probe scored 100% attack success against `qwen2.5:1.5b`, confirming `app/agent/moderation.py`'s pattern screen is the real line of defense, not a redundant layer. A local promptfoo redteam run (small local model as both generator AND grader) produced a misleading 93%-failed headline traced to malformed generated prompts and a grader confabulating evidence — moved `redteam.provider` to a cloud judge (`gemini-3.1-flash-lite`, later also `openai/gpt-oss-120b` via Groq for one deepeval file) once local-model-as-judge unreliability was independently reproduced three separate ways (promptfoo, deepeval's `FaithfulnessMetric`, `AnswerRelevancyMetric` all failed to discriminate a good answer from a bad one on a small local judge). The target model stays local always — only the grader/generator role moved to a cloud judge, kept manual (`make promptfoo-redteam`) rather than gated in CI.
- **Real redteam findings, fixed same-day**: a real prompt-extraction leak (asked to "act as a senior auditor," the model echoed real system-prompt paragraphs) and a real sandbox-safety miss (asked to "wipe temp files" under an urgency pretext, the model authored a real destructive script) were both found via Gemini-judged redteam runs and fixed with explicit prompt-level scope/anti-disclosure rules — re-verified to hold against fresh adversarial variants, with a couple of narrower paraphrase-based gaps disclosed rather than chased further (a known ceiling of prompt-only defense on a small model, not a fixable wording bug).
- **deepeval** (`tests/deepeval/`, `@pytest.mark.deepeval`, `make deepeval`) adds a genuinely different question none of the above ask: is an answer's own claim actually entailed by its cited context (`FaithfulnessMetric`/`AnswerRelevancyMetric`), did the agent call the right tool with sensible arguments (`ToolCorrectnessMetric`/`ArgumentCorrectnessMetric`), and does a multi-turn conversation hold up (`ConversationSimulator`, `RoleAdherenceMetric`/`KnowledgeRetentionMetric`). Runs in CI as a non-blocking signal (deepeval's own `flaky=True`, which still lets a genuine crash fail the job) rather than a gate. An optional in-process backup judge (Plugsky, free NVIDIA Nemotron) now absorbs a primary judge's own 429s mid-call, on top of an existing test-level rerun-on-transient-error policy.
- **Real-world**: any CI setup wanting genuine Docker-backed coverage under real test-runner parallelism without serializing everything or paying for N copies of an expensive shared resource.

### 49. **Per-Domain Requests Streams: One Unified API Serving Every Domain's Queue (`queue.py::requests_stream_key`, `app/api/main.py::get_domain`)**
Closes the gap where the queued API endpoints published every job onto one flat `agent:requests` stream regardless of domain, so the built-in web UI could only ever reach whichever domain a worker pool happened to be running. `requests_stream_key(domain)` (`agent:requests:{domain}`) gives each domain its own consumer group; `get_domain` reads the caller's `X-Domain` header (default `"ecorp"`) and routes onto that domain's stream — one unified API process now reaches every domain a worker pool is currently running for.
- **Domain is inferred from which stream a message landed on**, never carried in the payload — a resume/cancel job must land on the same domain's stream the original turn did, since the paused graph was compiled from that domain's manifest/tools.
- **An unknown `X-Domain` fails loud (422)** rather than publishing onto a stream nothing reads, which would hang silently.
- **Real-world**: one SaaS product fronting several distinct bots, wanting one public API surface while scaling each bot's own execution capacity independently.

### 50. **Real Web Crawling (crawl4ai) and a Real Sandbox (OpenSandbox over MCP, Wrapped Narrow)**
Closes two gaps: `ingest_url` was a bare `httpx.get` + HTML-strip (useless against JS-rendered sites); `calculator` was a narrow AST evaluator with nowhere safe to run arbitrary code. crawl4ai is a first-class **in-process dependency** (`app/ingestion/web_crawler.py`) so its output flows through this app's own SSRF guard and stores. OpenSandbox is consumed entirely **over MCP** (pattern 28's unmodified `load_remote_tools`), via a real `opensandbox-mcp` bridging to a separately-run `opensandbox-server`.
- **One shared SSRF guard**, extracted to `app/core/url_safety.py`, used by both static-page ingest and crawl4ai's real-browser render.
- **Every one of these tools is `"outward"`** — the mandatory approval gate fires unconditionally, same tier as `post_to_team_channel`.
- **A real live-verified model-capability finding that changed the design**: handing the LLM OpenSandbox's raw ~19-tool catalog directly caused `qwen2.5:3b` to hallucinate a `sandbox_id` and loop on the wrong recovery tool — three escalating prompt fixes didn't change its very first action. Fixed structurally, not with more prompt text: `app/domains/sandbox_session.py` hides the whole lifecycle behind three flat tools (`run_command_in_sandbox`, `read_sandbox_file`, `write_sandbox_file`), doing sandbox lookup/creation in code (keyed by `thread_id` metadata, looked up server-side so it survives across horizontally-scaled workers) — re-verified live to fully eliminate the original failure mode.
- **A later structural fix for a persistent shell-quoting ceiling**: the model kept producing a different broken `python -c '...'` one-liner each time despite repeated docstring warnings. `run_python_in_sandbox` eliminates the failure class by passing the script as a plain string argument (never through a shell), rather than warning about quoting again.
- **`check_output` gained two new categories from live findings**: `fabricated_tool_output` (the model presents an invented "tool result" — code and output — with no real `tool_calls` behind it; ranked above other retry reasons since presenting false information as true is worse than failing to act) and `skipped_required_tool` (a skill said to use a specific tool, and the model estimated the answer in its head instead) — both backed by a proactive reminder plus a reactive catch, not the reminder alone.
- **A real streaming-corruption bug**: two simultaneous tool calls from one model turn occasionally came back from the backend as one corrupted, glued-together `tool_calls` entry — fixed with `parallel_tool_calls=False` on `bind_tools()`.
- **A tool-list-ordering finding, not a model ceiling**: sales' skill-based math question never routed through `skill_search` first despite three escalating prompt fixes — root cause was list POSITION (`skill_search`/`use_skill` landed at the end of every domain's bound tool list), not wording; reordering them to the front fixed it in every domain, confirmed 3/3 and 2/2 on fresh runs. **Lesson**: check the mundane structural explanation (tool order, schema shape) before concluding "the model just can't do this."
- **A cluster of real infra "it's the environment" misdiagnoses, each corrected by tracing the actual request**: a mysterious `sandbox_create` 405 was eventually traced to a PORT COLLISION with this repo's own `open-webui` (both defaulted to 8080), not an OpenSandbox bug; a mysterious client-side timeout on sandbox reuse was traced to the OpenSandbox SDK trying to reach a Docker-bridge-internal IP unreachable from the host once the server was containerized, fixed via a `use_server_proxy=True` wrapper script (`scripts/opensandbox_mcp_bridge.py`), not a longer timeout. Both times, tracing the real network call — not accepting a plausible "it's just flaky infra" story — found a categorically different, fixable bug.
- **Real, disclosed third-party/environment bugs, left as documented workarounds, not hidden**: `read_sandbox_file` reliably 404s immediately after a real successful write (an `opensandbox-mcp`/`server` gap, not this app's code) — every domain's docstring now says to fall back to `cat <path>` via `run_command_in_sandbox`. A non-root sandbox image needed an explicit `WORKDIR` for relative-path writes to work. Telegram's `telegram:{chat_id}` thread ids broke every sandbox metadata call (OpenSandbox's metadata charset forbids `:`) — fixed by sanitizing only at the sandbox-metadata boundary, not changing `thread_id`'s format everywhere else it's used (the Postgres checkpointer key, Langfuse trace metadata).
- **What this deliberately doesn't attempt**: tenant/`SecurityCtx` scoping of the OpenSandbox catalog itself (a shared operational resource, not tenant data — the approval gate is the real boundary); cross-thread sandbox reuse beyond a TTL; a file-listing tool.
- **Real-world**: any agent needing live, JS-rendered web access or real isolated computation an LLM's own arithmetic can't reliably do — the general shape (gated behind mandatory approval, consumed over MCP) generalizes past OpenSandbox specifically.

## Graph Flow

```
START
  ↓
validate_input  ← also resets iterations/total_tokens/run_id,
  ↓                stamps SecurityCtx from config["configurable"]["ctx"]
route_after_validation?  ← Decide: valid ctx? is the last HumanMessage non-empty?
  ├─→ reject_context  (no valid tenant+principal — checked FIRST, see pattern 17)
  │    ↓
  │   END
  │
  ├─→ reject_input  ← AIMessage explaining the problem
  │    ↓
  │   END
  │
  └─→ compact_history  ← trims turns past HISTORY_TOKEN_CEILING (down to
       │                  HISTORY_TOKEN_FLOOR) AND folds them into a
       │                  cumulative history_summary via one LLM call
       │                  (pattern 41); no-op when within budget
       ↓
      route_after_compaction?  ← Decide: is history_summary itself still
       │                          over MAX_HISTORY_SUMMARY_CHARS?
       ├─→ context_window_exceeded  (a named dead end, not silent truncation)
       │    ↓
       │   END
       │
       └─→ moderate_input  ← real pattern-based screen for known injection/
            │                 jailbreak phrasings + a small denylist (pattern 25);
            │                 fail-open only on the CHECK's own failure, never on
            │                 a real hit
            ↓
           route_after_moderation?  ← Decide: screened out?
            ├─→ reject_moderation  ("I can't help with that request.")
            │    ↓
            │   END
            │
            └─→ check_semantic_cache  ← tenant+principal-scoped cosine-KNN lookup in
                 │                       Redis (app/retrieval/semantic_cache.py, pattern 22);
                 │                       degrades to a miss on any failure
                 ↓
                route_after_cache?  ← Decide: near-identical query cached already?
                 ├─→ check_output   (HIT — cached answer appended as a final
                 │    ↑               AIMessage, no retrieval, no LLM call at all)
                 │    │
                 └─→ retrieve_context  ← MISS: enrich — hybrid dense+BM25 search,
                      │                  RRF-fused, cross-encoder reranked, numbered
                      │                  citations (pattern 20); + this principal's
                      │                  memories; degrades on failure; both
                      │                  tenant/owner-scoped via app/core/security.py's Policy
                      ↓
                     agent  ← Think: call LLM with context injected as a delimited,
                     │        untrusted <retrieved_document> SystemMessage (retried on
                     │        transient LLM failure via AGENT_RETRY_POLICY). Can call
                     │        ask_clarification (an ordinary read_only tool, pattern 27)
                     │        when a request is materially ambiguous instead of guessing.
                      ↓
                     should_continue?  ← Decide: tools? too many at once? approval needed? done? over budget?
                      ├─→ too_many_tool_calls  (> MAX_TOOL_CALLS_PER_TURN at once)
                      │    ↓
                      │    agent  ← Loop back with synthesized ToolMessage rejections
                      │
                      ├─→ human_approval  (require_approval=True on input state, OR any
                      │                    pending tool call is non-read_only — mandatory,
                      │                    see TOOL_CAPABILITIES)
                      │    ↓
                      │   route_after_approval?  ← interrupt() paused here for a decision
                      │    ├─→ tools       (approved)
                      │    ├─→ agent       (rejected — with synthesized ToolMessage rejections)
                      │    └─→ __end__     (cancelled — checked FIRST, never loops back, pattern 36)
                      │
                      ├─→ tools  ← Execute tool calls (concurrently, if there are several) —
                      │    │       may include remote MCP tools (app/mcp/client.py, pattern 28)
                      │    ↓       bound into a DomainPlugin like any other tool
                      │    agent  ← Loop back to think again
                      │
                      └─→ check_output  ← also both cache-hit AND cache-miss paths
                           │               rejoin here (see above); computes
                           │               used_citations AND ungrounded_claims_count
                           │               from the answer text (pattern 20, pattern 39)
                           ↓
                          route_after_check?  ← Decide: is the answer too short?
                           ├─→ retry_output  ← Append corrective HumanMessage
                           │    ↓
                           │    agent  ← Loop back with feedback
                           │
                           └─→ suggest_followups  ← 2-3 follow-ups from a GROUNDED
                                │                     answer only (used_citations
                                │                     non-empty); skipped on a cache
                                │                     hit or an uncited answer (pattern 27)
                                ↓
                               write_semantic_cache  ← writes query/answer/citations
                                │                       back UNLESS this turn was
                                │                       itself a cache hit (no-op then)
                                ↓
                               END
```

`should_continue` also ends the run early (→ `__end__`, skipping `check_output`) on any of three independent safety budgets: the iteration cap, the token-per-turn cap (`MAX_TOKENS_PER_TURN`), the dollar cost ceiling (`MAX_COST_USD_PER_TURN`, pattern 35), or `MAX_REPEATED_ACTIONS` identical consecutive tool-call batches (pattern 34, no-progress detection) — none of these are shown as their own branch above to keep the diagram legible, but each is a real, independently-tested exit.

## Differences from Basic Agent

| Aspect | Basic | Enhanced |
|--------|-------|----------|
| **State** | Just messages | + context, citations, used_citations, ungrounded_claims_count, cache_hit, moderation_blocked, followups, history_summary, iterations, total_tokens, total_cost_usd, run_id, graph_version, state_schema_version, ctx, require_approval, approved, cancelled |
| **Nodes** | agent + tools | 18 nodes: validate_input, reject_input, reject_context, compact_history, context_window_exceeded, moderate_input, reject_moderation, check_semantic_cache, retrieve_context, agent, tools, human_approval, too_many_tool_calls, check_output, retry_output, suggest_followups, write_semantic_cache |
| **Flow** | LLM ↔ tools loop | Multi-stage pipeline: history compaction, a moderation screen, and a cache short-circuit up front, plus nine real conditional gates |
| **Safety screening** | None | Pattern-based moderation before retrieval, cache, or any spend — a real hit fails closed, the check's own failure fails open (pattern 25) |
| **Context** | LLM decides what to search | Pre-fetched (hybrid dense+BM25, RRF-fused, cross-encoder reranked, pattern 20 — plus retention-filtered memories, pattern 33), injected, delimited as untrusted (pattern 12), tenant/owner-scoped (pattern 17) |
| **Answers** | Plain text | Numbered inline citations, filtered post-hoc to only markers actually used (pattern 20), an independently-computed ungrounded-claims count (pattern 39); clarifies instead of guessing when ambiguous, offers grounded follow-ups otherwise (pattern 27) |
| **Isolation** | None — one shared corpus | Every read/write scoped to `SecurityCtx` via a store-level pre-filter, never a Python post-filter (pattern 17, 22) |
| **Safety** | No loop limit | Nine independent budgets (pattern 10) — plus graceful degradation on retrieval/cache/follow-up/compaction failure, automatic LLM-call retry, and friendly tool-error messages instead of crashes |
| **Output** | Whatever LLM says | Validated, with an actual retry path back to `agent` |
| **Tools** | Two read-only tools | Five read-only (`search_docs`, `calculator`, `query_employees`, `ask_clarification`, remote MCP tools) + two mutating (`add_note`, `remember`), each declaring a capability (pattern 15); every result passes credential scrubbing (pattern 32) |
| **Tool calls** | Run immediately | Run immediately (parallel if requested) *or* pause for approval — opt-in for read-only, **mandatory** for mutating/outward — with a genuine cancellation outcome (pattern 36) |
| **Conversation history** | Unbounded, or naive truncation | Trimmed past `HISTORY_TOKEN_CEILING` (hysteresis, not sliding window) with discarded turns folded into a cumulative summary, not just dropped (pattern 41) |
| **Memory** | None, or unscoped | Cross-session, write-gated, re-filtered against ctx and a retention horizon on every recall (pattern 18, 33), with audited deletion |
| **Repeat queries** | Full retrieval + LLM call every time | Tenant+principal-scoped semantic cache short-circuits a near-identical query to the cached answer (pattern 22) |
| **Ingestion** | Whatever the example seeded once | A general-purpose `Ingestor` (files/URLs/text), parent-child chunking, SSRF-guarded fetch (pattern 24) |
| **Checkpointing** | Usually none, or `MemorySaver` | `AsyncPostgresSaver` for the real singleton — survives a restart and concurrent worker access, with build/schema versioning (pattern 16) |
| **Persistence** | Whatever the example used | A pooled `ConnectionPool` (pattern 31), transparent to every call site |
| **Observability** | None | Langfuse tracing + OTel metrics via OTLP (pattern 11), structlog lifecycle logs (pattern 14), per-tool-call audit records (pattern 37), a real usage-cost ledger (pattern 26, 38), an optional Grafana/Loki/Prometheus stack |
| **Errors** | Whatever the framework raised | A canonical `{code, message, details}` envelope (pattern 30) |
| **Regression detection** | None | `tests/` (fake-LLM) + `scripts/eval.py` golden dataset with repetition/grounded-claims release gates (pattern 40) |
| **Domains** | One hardcoded agent | One graph, adapted per domain via `AgentManifest` + `DomainPlugin` (pattern 23) |
| **Interface** | Whatever the example used | CLI + HTTP API + web UI (pattern 29) + Telegram (pattern 42), all on the same streaming core |
| **Scaling model** | One process does everything | Queued via Redis Streams (pattern 43) — scale agent workers independently of SSE connections |
| **Input** | Text only | Text, optionally with images (pattern 44) |

## When to Use Each Pattern

- **Validation**: Always. Costs nothing, catches most bad cases.
- **Context enrichment**: When you have a knowledge base (Qdrant, DB, API).
- **State tracking**: When you need loop control, retries, or multi-step logic.
- **Output gating**: When answer quality matters (not every use case needs it).
- **Conditional routing**: When different queries need different paths (most real agents).
- **Error recovery**: Any tool that can fail — cheap to add via `handle_tool_errors`, so default to having it.
- **Human-in-the-loop**: Side-effecting or costly actions. Not needed for read-only tools. For a tool that writes/sends/spends, don't make the gate opt-in — declare its capability and let pattern 15 make it mandatory.
- **Parallel tool execution**: Automatic — nothing to opt into.
- **Multi-layer safety budgets**: Any agent talking to a real model/tools over a network. Tune the constants to your model/traffic; the pattern (several narrow budgets, not one big one) is what matters.
- **Custom metrics**: As soon as this agent has more than one user.
- **Golden-dataset evaluation**: As soon as you're tempted to change the prompt/model/retrieval "just to see."
- **Untrusted content framing**: Any time content the model didn't type re-enters the prompt. Cheap and structural — no reason to skip it.
- **Bounded conversation history**: Any agent with multi-turn threads that can run for a while.
- **Node telemetry**: As soon as this runs somewhere you can't attach a debugger — almost immediately.
- **Tool capability declarations**: The moment a second tool exists — retrofitting after several mutating tools accumulate means auditing all at once instead of one at a time.
- **Durable checkpointing**: As soon as any HITL gate is reachable in a deployment that restarts while a human might be reviewing.
- **Multi-tenant isolation**: The moment more than one customer/workspace shares a deployment — do this before anything else on this list, since most other patterns here quietly assume a security boundary exists.
- **Cross-session memory**: Any agent worth a "remember this" conversation. Skip if every session is truly stateless.
- **Prompt-cache stability**: As soon as more than one principal's traffic shares a deployment AND the provider offers prefix caching.
- **Hybrid retrieval + rerank + citations**: As soon as retrieval quality/recall matters (not a toy corpus) and an answer needs to be auditable.
- **Fixed-tool structured data access**: The moment an agent needs to answer questions against a relational store. Never build text-to-SQL as the first move.
- **MCP exposure**: When a tool needs to be reachable by a different agent/client, not just this app's own loop. Treat it as its own trust boundary.
- **Semantic cache**: Once near-duplicate questions repeat across users/sessions often enough to matter.
- **Config-first multi-domain composition**: The moment a second, genuinely different use case needs the same hardened graph but different tools/prompt/policy.
- **General-purpose ingestion + chunking**: As soon as this needs to be a product someone can point at their own content.
- **Input moderation**: Any input surface reachable by someone not fully trusted — most of them.
- **A real usage/cost ledger**: The moment more than one team/customer shares a deployment and "how much, by whom" needs an answer later.
- **Clarification and follow-ups**: Clarification where a wrong guess is expensive; follow-ups on consumer-facing chat surfaces.
- **Consuming a remote MCP tool catalog**: When a useful tool already exists as an MCP server you don't own. Always pair with explicit `capability_overrides`.
- **A built-in web UI**: As soon as "standalone product" is the actual claim, not "library other things call."
- **A chat-platform channel**: When your users already live in a chat app.
- **A Redis Streams queue**: The moment SSE-connection concurrency and LLM-call concurrency need to scale on different axes.
- **Multimodal/image support**: Any agent whose users have a real reason to ask about an image — verify vision AND tool-calling capability before assuming an alias swap is enough.
- **Canonical error envelope**: As soon as more than one caller needs to distinguish failure modes programmatically.
- **DB connection pool**: Any deployment issuing more than a handful of queries/sec.
- **Credential/secret scrubbing**: Any agent whose tools touch a system that could legitimately contain secrets.
- **Memory deletion audit + retention**: Any product storing personal data under a retention policy.
- **No-progress detection**: Any tool-calling agent against a real, imperfect model.
- **Cost ceiling enforcement**: Any agent on a metered, real-dollar-cost model API.
- **Run cancellation**: Any HITL-gated agent long-running enough that a human might want to stop it outright.
- **Structured per-tool-call audit record**: As soon as "which tool, with what, did it succeed" needs to be answerable from logs alone.
- **Resolved concrete model recording**: Any deployment behind a model gateway/router.
- **Ungrounded claims count**: Any RAG system where citation accuracy needs to be measurable, not spot-checked.
- **Eval statistical rigor**: As soon as a golden-dataset eval exists against a non-deterministic model.
- **Bounded history summarization**: Any long-running conversational thread where losing early context outright would cost something real.

## Extending Further

In production, you might still add:
- **Fallback node**: If primary path fails, try alternative.
- ~~A real HTTP resume flow~~ — **done**: `POST /chat/resume` is the HTTP counterpart to `astream_events_resume`, routed through the same Redis Streams queue (pattern 43).
- ~~Grafana dashboards / alerting~~ — **done**: `docker-compose.observability.yml` (`make obs-up`) — Grafana + Loki + Prometheus + Alertmanager + otel-collector, two provisioned dashboards, a starter alert rule set (see pattern 11).
- **A real vision-AND-tool-calling-capable local model** (pattern 44): every small local Ollama vision model tried supports vision OR tools, never both — `litellm-config.yaml`'s `vision` alias is a ready slot, not a working default.
- **Image-aware moderation and Telegram/CLI image input**: moderation screens text only; Telegram/CLI have no UX for attaching an image at all, even though every server-side entry point carries one correctly.
- ~~Fault-tolerant redelivery for the Redis Streams queue~~ — **done**: `queue.py::reclaim_stale_entries` (`XAUTOCLAIM`), run periodically by both `agent_worker.py` and `ingest_worker.py`'s own `_reclaim_loop`, finds entries abandoned by a worker that died mid-job. Never redelivers blindly — `agent_worker.py::_handle_reclaimed_job` proves per job whether it's safe: `"cancel"` and `"resume"` jobs are always safe now (see below); a `"turn"` job is safe only if its checkpointed state (`_is_safe_to_retry_turn`, via `graph.aget_state` — never re-runs anything) shows no `mutating`/`outward` tool call completed yet since its own last `HumanMessage`. Safe jobs are silently republished (`queue.py::republish_job`, capped by `MAX_AUTO_RECLAIM_RETRIES`); everything else (a `"turn"` that already ran a mutating/outward call, every ingest job — no cheap safety proof exists for ingest yet) surfaces a `WORKER_LOST`/error event on the job's own results stream and is archived to a dead-letter stream (`queue.py::dead_letter_stream_key`) for manual inspection/replay. `agent_worker_job_reclaimed_total{queue,outcome}` distinguishes `retried` from `dead_lettered` — any nonzero rate still means workers are crashing, `outcome` just says whether that's self-healing.
- ~~Idempotency keys on mutating/outward tools~~ — **done**: `app/agent/tool_idempotency.py::idempotent` wraps every `mutating`/`outward` tool call (`add_note`/`remember`, every sales/support/ops write tool, all four sandbox tools) — keyed by the LLM-provider-assigned `tool_call_id` (`InjectedToolCallId`), persisted in Postgres (`postgres-init/13-tool-call-dedup.sql`). A second invocation under the same id returns the first's own cached result instead of running the real side effect again. This is what makes a reclaimed `"resume"` job (above) safe to retry unconditionally: resuming re-invokes whichever tool calls were pending at the `human_approval` pause under their ORIGINAL tool_call_ids, so anything that already ran just dedupes. Deliberately does NOT make a retried `"turn"` safe past its own mutating-tool check — a fresh `astream_events_turn` call re-asks the LLM, which gets brand-new tool_call_ids unrelated to the crashed attempt's own, so dedup can't catch that case; `_is_safe_to_retry_turn` still exists for exactly that gap. Fails open (`agent_tool_dedup_degraded_total`) on its own storage failure, same posture as `usage_ledger.py`/`sessions.py`'s own `get_connection()` callers — a dedup-store outage must never block a mutating tool call outright.
- **Auto-scaling / crash-restart for the worker pool**: `docker compose --profile app up -d --scale agent-worker=3` works and each worker handles `SIGTERM` gracefully, but a real orchestrator that restarts a crashed worker and scales on queue depth is still out of scope.
- **A real webhook-based Telegram deployment**: long-polling needs no inbound port, the right default for a demo; production would switch to `setWebhook` without changing the core underneath.
- **Real authentication**: pattern 17's `SecurityCtx`/`Policy` is the isolation structure; `app/api/main.py`'s trusted-header extraction deliberately isn't authentication — a production deployment adds a real auth gateway in front of it.
- **Per-action authorization within a tenant**: today every principal in a tenant has the same write capability the gate allows at all — a finer-grained `Policy` reading `ctx["claims"]` would live here.
- ~~`app/api/main.py` reading which domain a request is for~~ — **partly done** (pattern 49): the queued endpoints route per-request via `X-Domain`. Still unbuilt: a real multi-domain runtime — `app/agent/runtime.py`'s CLI/API singleton still only ever builds one graph for one domain; a single process serving several domains directly (not via a queue to an external worker) would need a per-domain graph registry and a seeding cache keyed by `(domain, thread_id)`.

The key insight: **LangGraph lets you make every step of the pipeline explicit and controllable.** That's what separates it from "just LLM + tools."
