# LangGraph patterns

Fifty patterns the agent in `app/agent/` applies, each with the bug or gotcha that motivated it. Numbers
are stable: code and tests cite them ("pattern 43"). **Bug** marks a real defect that was found and fixed;
**Gap** marks something disclosed and left open.

| Concern | Patterns |
|---|---|
| Agent loop | 1–7, 9, 10, 34, 35, 39 |
| Approval and governance | 8, 15, 36 |
| Tenant security | 12, 17, 25, 30, 32 |
| Retrieval, memory, caching | 2, 13, 18–20, 22, 24, 33, 41 |
| Tools and data | 21, 27, 28, 31, 45, 46, 50 |
| Reliability and scaling | 16, 43, 49, and [exactly-once](#extending-further) |
| Observability and cost | 11, 14, 26, 37, 38 |
| Interfaces and extensibility | 23, 29, 42, 44, 47 |
| Quality gates | 40, 48 |

## Patterns

### 1. Input validation with a real exit
`validate_input` stamps state; the conditional edge `route_after_validation` sends bad input to `reject_input` (an `AIMessage`: the system speaking, not the user) and on to `END`. Valid input continues to `compact_history`.
- **Use when:** always. It costs nothing and catches most bad cases.

### 2. Context enrichment
`retrieve_context` pre-fetches before the LLM reasons. `agent` appends the result as a `SystemMessage` just before the call; the base `SYSTEM_PROMPT` is seeded once per thread (`runtime.py::_ensure_seeded_async`).
- **Use when:** you have a knowledge base (Qdrant, a DB, an API).

### 3. State tracking
Flow control lives in explicit state (`iterations`, `context`, …), which is what makes loop limits, retries and conditional routing possible.

### 4. Conditional routing
`should_continue` is an edge function, not a node: it decides where execution goes after `agent` (tools, approval, budget exit, or done).

### 5. Output gate with a real retry
`check_output` + `route_after_check` + `retry_output`. A suspiciously short answer routes to `retry_output`, which appends a corrective `HumanMessage` and loops to `agent`. `MAX_ITERATIONS` bounds the retries. Pattern 50 adds two retry reasons from live findings.

### 6. Loop control
`MAX_ITERATIONS` is the shared cap for both the tool loop and the output-retry loop.

### 7. Error recovery
`ToolNode(handle_tool_errors=_friendly_tool_error)` turns a tool exception into a `ToolMessage` the agent sees next turn instead of killing the run. Three failure modes, three policies:
- A tool fails mid-turn: recover via that message.
- `retrieve_context` fails: **degrade** to no pre-fetch (the LLM still has `search_docs`).
- The `agent` node's LLM call fails: automatic **retry** (`AGENT_RETRY_POLICY`). LangGraph's default `retry_on` excludes programming errors, so retry can't mask a bug.

### 8. Human-in-the-loop
`human_approval` calls `interrupt()`, which suspends the node and persists state through the checkpointer; a caller resumes with `Command(resume=True/False)`. Two routes reach it: the opt-in `require_approval` flag, and the mandatory route for any non-`read_only` call (15). `make chat-hitl` is a runnable example.
- **Gotcha:** a rejection still needs a matching `ToolMessage` per pending `tool_call`, or the next LLM call fails. `human_approval` synthesizes them.
- **Unattended callers** (`astream_events_turn_unattended`: Telegram, fire-and-forget jobs) auto-decline with `Command(resume=False)`.
- **Bug:** it declined only once, so a model that re-requested the gated write left the checkpoint paused: an empty reply, then every later message refused as "pending approval". It now declines up to `UNATTENDED_MAX_DECLINE_ROUNDS` (3), then cancels the run and sends one explicit "needs a person's approval" message. Test: `test_agent_pause_handling.py::TestUnattendedSecondPause` (earlier tests mocked the stream and never drove a second pause).
- **Use when:** an action sends, writes, spends or deletes.

### 9. Parallel tool execution
`ToolNode` already runs several tool calls from one LLM turn concurrently. No fan-out node is needed (LangGraph's `Send` is for fanning one node over dynamic graph branches, a different case).

### 10. Layered safety budgets
`MAX_ITERATIONS` alone is one layer. Each budget below catches a failure the others can't.

| Budget | Where | Catches |
|---|---|---|
| `MAX_ITERATIONS` | `should_continue` | Model stuck calling tools |
| `MAX_TOOL_CALLS_PER_TURN` | `should_continue` | One turn fanning into dozens of calls |
| `MAX_TOKENS_PER_TURN` | `should_continue`, `agent()` | Unbounded spend over few iterations |
| `HISTORY_TOKEN_CEILING`/`FLOOR` | `compact_history` | History growing without bound (13, 41) |
| `MAX_COST_USD_PER_TURN` | `should_continue`, `agent()` | Same tokens, different dollars by model tier (35) |
| `MAX_REPEATED_ACTIONS` | `should_continue` | The same call with the same args, repeatedly (34) |
| `TOOL_TIMEOUT_SECONDS` | Per tool call | A hung Qdrant or embedding call |
| `REQUEST_TIMEOUT_SECONDS` | Whole turn | Many slow-but-not-hung steps |
| `RECURSION_LIMIT` = `MAX_ITERATIONS * 2 + 15` | LangGraph step cap (`runtime.py`) | A routing bug causing a node cycle |

- **Bug:** `iterations` and `total_tokens` persist across a thread, so `validate_input` resets them each turn (not on a resume).
- **Bug:** a reset only works if the field's reducer allows it. `subagent_spend` used `operator.add`, so "reset to `[]`" did nothing and old spend counted against later turns; `cancelled`/`approved` weren't reset at all (36). The test asserted the return value, which can't see a reducer. Assert on state read back across two turns (`test_safety_budgets.py::TestPerTurnResetThroughTheGraph`).
- **Use when:** any agent talking to a real model and tools. The pattern (several narrow budgets) matters more than the numbers.

### 11. Custom metrics
OpenTelemetry counters and histograms (`app/core/metrics.py`, `telemetry.py`) for the "how often, across everyone" view that Langfuse's per-call traces can't give.
- **Push, not pull:** every process pushes OTLP to one otel-collector, so one scrape target covers the API and all workers. A `/metrics` on the API could never see a worker.
- **Wiring:** `MetricsCallbackHandler` for tool calls with no node changes; direct `.inc()` where a node decides or degrades. Histogram buckets are explicit: OTel's defaults put every 1–90 s turn in one bucket.
- **Bug: scheduled jobs exported nothing.** `ops_digest`, `followup_sweep`, `tool_call_dedup_sweep` and `ops_investigate` exited before the 15 s export timer, so counters died with the process, and a failing digest post could never fire `TeamChannelNotifyFailing`. Each now runs in `job_runtime.py::scheduled_job()`, which flushes in a `finally`.
- **Bug: a Langfuse client per turn.** Each client starts three background threads nothing stops (six per turn), and the flush hit a fresh client's empty queue. Now one shared client per process (`app/core/tracing.py`), flushed once at exit.
- **Gap:** long-lived processes don't flush on graceful shutdown (up to one export interval lost); a SIGKILL loses unflushed metrics and a fraction of a second of traces.
- **Use when:** as soon as there is more than one user.

### 12. Untrusted content framing
Retrieved text is wrapped in `<retrieved_document>` delimiters, and `SYSTEM_PROMPT` says delimited content is data, not instructions. It's structural, so the model needn't spot an attack. One function does all framing, `app/core/untrusted.py::frame_untrusted`: the pre-fetch, the three page-reading tools, and `package_lead_brief`'s replay of stored CRM notes.
- **Bug:** only the pre-fetch wrapped anything. Page readers returned raw text, and `enrich_lead_from_website` stored up to 20,000 characters as a CRM note that `package_lead_brief` (read-only, so un-approved) replayed unframed: 59,150 characters after three enrichments. Separately, a page containing `</retrieved_document>` closed the frame early.
- **Fix:** tag-shaped sequences inside the text are neutralised (still visible, no longer a tag). Notes are framed at replay and bounded to the newest `BRIEF_NOTES_MAX_CHARS`.
- **Limits:** it can't stop a model that follows what's inside; the approval gate does (Principle II). Not covered: ticket comments, `check_ticket_status` notes, a per-note replay cap.
- **Use when:** content the model didn't type re-enters the prompt.

### 13. Bounded conversation history
`messages` is the one `State` field with no cap by construction. `compact_history` (right after `validate_input`) trims via `RemoveMessage` with an estimated-token hysteresis: no-op at or under `HISTORY_TOKEN_CEILING`; once exceeded, drop the oldest whole turns until at or under the lower `HISTORY_TOKEN_FLOOR`.
- **Hysteresis, not "trim back to N":** trimming to the same ceiling re-triggers summarization on nearly every turn and shifts every later token, invalidating the provider's prefix cache.
- **Tokens, not turns:** turns vary wildly; a `tiktoken` estimate (approximate) tracks the context window.
- **Turn-aware:** a turn runs from a `HumanMessage` to the next, so a `tool_call`/`ToolMessage` pair is never split (an orphan fails the next LLM call). The system prompt and latest turn are never dropped. Dropped turns are folded into a summary (41).

### 14. Node telemetry
`build_graph` wraps every node (never inside the node) in `_instrumented`, which logs `node_started` then `node_completed`/`node_failed`/`node_paused` with the node name, a per-turn `run_id` and `duration_ms`. `human_approval`'s `GraphInterrupt` is a normal pause, logged as `node_paused` and re-raised untouched.
- **Never logged:** message content or the `state` dict, so this can't become an unscrubbed second copy of prompt text outside Langfuse.
- `run_id` is the join key across a request's logs and metrics; Promtail ships the JSON to Loki.

### 15. Tool capability declarations: a mandatory gate
Every tool declares `read_only`, `mutating` or `outward` in `TOOL_CAPABILITIES` (`app/agent/tools.py`). `should_continue` checks every pending batch: any non-`read_only` call routes through `human_approval` **unconditionally**. A tool missing from the mapping defaults to `outward` (fail closed). No flag turns this off.
- **Why mandatory:** a RAG agent already carries untrusted content on nearly every turn (12); adding write capability is one gamble from a document steering a real write.
- **`add_note` is the worked example** of the companion rule: a write tool is a fixed, typed, closed-vocabulary operation, and its point id is derived by code (a fresh UUID, later `tool_call_id`-derived, see Extending Further), never supplied by the model, so it can only append.
- **Use when:** the moment a second tool exists. Retrofitting after several writers accumulate means auditing them all at once.

### 16. Durable checkpointing and cross-restart compatibility
`build_graph()` defaults to `MemorySaver` for tests; the runtime singleton uses `AsyncPostgresSaver` (its own database) so a paused approval survives a restart and several processes (API plus worker replicas) share one checkpoint store, which a SQLite file's writer lock can't do safely.
- **Async everywhere:** the saver's lock binds to the event loop that created it, so every process awaits `init_graph_async()` on its own loop.
- **`STATE_SCHEMA_VERSION` + `graph_version`:** each turn stamps a schema version (bumped only on a breaking `State`/topology change) and a build id. `resumability_error_async` runs before every resume and distinguishes `checkpoint_lost` (no paused run) from `checkpoint_incompatible` (schema mismatch). A differing build SHA alone is not an error: deploys change it constantly.
- **Use when:** a HITL gate must survive a deploy.

### 17. Multi-tenant isolation
A `SecurityCtx` (`tenant`, `principal`, `claims`) is stamped once by `validate_input` from `config["configurable"]["ctx"]`, never from message content. `Policy.lower(ctx, target)` becomes a Qdrant filter applied **inside the query**, never a Python post-filter: a buggy post-filter looks identical to a correct one until a cross-tenant leak.
- `documents` scope to `tenant`; `memories` also to `owner`, in one collection told apart by a `kind` field that is part of the filter.
- **Fail closed:** `route_after_validation` checks ctx first, and every ctx-aware tool re-checks it (a different code path).
- **Not authentication:** `get_ctx` trusts `X-Tenant-Id`/`X-Principal-Id`; a real auth gateway sets them.
- **Bug: conversations weren't isolated.** Thread state (checkpoint, cancel flag, lock, dedup key) is keyed by `thread_id` alone and only the `GET` endpoints checked ownership. Anyone naming an id could read it through the model, approve its pending action under their own identity, or cancel it. `_require_conversation_owner` now runs first on send, resume and cancel: a send atomically claims a new id (`claim_session`, `INSERT … ON CONFLICT DO NOTHING`) or verifies an existing one, and every refusal is the 404 an unknown id gets. It fails closed.
- **Gap:** the check is at the API; anything publishing to Redis directly bypasses it. `telegram:<chat id>` ids are reserved, but Telegram still shares one thread across a group chat. A conversation with no `chat_sessions` row can't be resumed over HTTP.
- **Use when:** more than one customer or workspace shares a deployment. Do this first.

### 18. Cross-session memory
`remember` is the only way a memory is written (`mutating`, gated like `add_note`). Recall is automatic, folded into retrieval: the model decides to *write*, never to *read*.
- **Why writing is opt-in:** whatever writes memory decides what replays into every future prompt, so an autonomous write would be a privileged side channel.
- **Re-filtered every call:** `recall_memories` applies `Policy.lower` fresh, scoped to tenant **and** owner, never cached.
- **Framed like a retrieved document, and more so:** a poisoned document affects one answer; a poisoned memory replays every turn until removed.
- **Not built:** an LLM-facing forget tool. `delete_by_filter` supports a data-subject-request script, not the model.

### 19. Prompt-cache stability
`SYSTEM_PROMPT` is a plain constant: no principal, tenant or timestamp is ever interpolated. `SecurityCtx` flows through `config`/`state["ctx"]`, never into the messages sent to the LLM. The classic cache-buster, `ctx["principal"]` in the prefix, looks stable within one conversation and only shows once a second principal shares the manifest. `tests/agent/test_prompt_cache_stability.py` asserts byte-identical rendering across two ctx values, plus a leak sweep.
- **Use when:** more than one principal's traffic shares a deployment behind a provider that discounts a stable prefix.

### 20. Hybrid retrieval, rerank and cited answers
`search_docs`/`recall_memories`/`retrieve_context` run `hybrid_search()` (`qdrant_store.py`): two Qdrant legs (dense via LiteLLM, BM25 sparse via in-process `fastembed`) fused server-side with RRF, then reranked by a cross-encoder that runs in the `ml-service` container (it was a measured concurrency bottleneck in-process). Every hit is numbered `[1]`, `[2]`, …; `check_output` scans the final answer for the markers it actually contains (`_used_citations`) and writes only those to state, **never trusting the model's claim about what it cited**.
- **Two degradation layers:** sparse failure falls back to dense-only (`agent_retrieval_degraded_total{stage="sparse"}`); reranker failure returns the RRF order (`stage="rerank"`). The answer is still fully cited.
- Citations are computed by intersecting the answer's markers with what retrieval offered, so a hallucinated or forgotten marker resolves correctly.
- **Use when:** retrieval quality matters and an answer needs an audit trail.

### 21. Fixed-tool structured data access, plus MCP exposure
`query_employees` is a closed, typed Postgres query: `tenant` (from `SecurityCtx`), `department` (a closed enum) and `name_contains` are the only variables, every query is parameterized and always ANDs `WHERE tenant = %s`. There is no `execute(sql)` anywhere. Same principle as `add_note` (15), for reads: the access boundary lives in reviewable code, not a string the model writes.
- **MCP exposure is a separate trust boundary.** `mcp/server.py` wraps the same query for external clients, but MCP has no `RunnableConfig`, so `tenant`/`principal` are explicit arguments checked by the same fail-closed `Policy.permit`. It does not authenticate the caller; a production server would derive identity from the client's verified auth.
- **Use when:** HR lookups, order status, inventory counts, wherever text-to-SQL would be tempting and exactly wrong.

### 22. Semantic cache
`check_semantic_cache` does a cosine-KNN lookup in Redis Stack scoped to tenant **and** principal (a cached answer can cite a principal's memories). A hit at or above `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` (0.95) skips retrieval and the LLM and rejoins at `check_output`; `write_semantic_cache` stores confirmed-final turns that weren't hits. Every cache call degrades to a miss on failure: a latency optimization, never a correctness dependency (`app/retrieval/semantic_cache.py`).
- **Bug: it cached turns that acted.** After an approved write, a near-identical request got "Ticket #123 created" with no model call, no approval and no write, because a hit never reaches the gate. Found when a browser test that repeated a prompt got the cached answer instead of an approval prompt. The write node now skips any turn that called a non-`read_only` tool. Not covered: an answer quoting live read-only data can be stale until its TTL.
- **Bug: tag escaping.** RediSearch treats `-` as syntax, and the first fix (a list of known characters) missed `|`: principal `alice|bob` built a filter that also matched `bob`'s entries, an isolation break. `_escape_tag` now escapes every ASCII character that isn't a letter, digit or underscore: a rule, not a list. Tests: `test_semantic_cache_tag_escaping.py` and a real-Redis integration test.
- **Use when:** near-duplicate questions repeat across sessions.

### 23. Config-first multi-domain composition
`build_graph(manifest=…, domain=…)` keeps its topology and adapts to a domain through an `AgentManifest` (config: name, system prompt, exposed tools) and a `DomainPlugin` (code: tool implementations, capabilities, policy). There is no `if domain == "…"` in `graph.py`; `DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN` wrap the Ecorp setup.
- **Port-swapping, not parameterization:** `should_continue` needs a domain's capability map but LangGraph calls it with only `state`, so it is bound with `functools.partial(should_continue, tool_capabilities=…)` once per build (with a default, so existing callers are unaffected).
- **The load-bearing test:** `tests/agent/test_manifest.py` builds a domain with a different `Policy` and one Ecorp-unknown tool, proves the unmodified gate treats it as `mutating` using that domain's map, and checks Ecorp's tools are absent from its `ToolNode`.
- **Gotcha:** `manifest.py` needs `graph.py`'s `SYSTEM_PROMPT` and `build_graph()` needs `manifest.py`'s defaults; a deferred import inside `build_graph()` breaks the cycle.
- **Not done:** several domains from one process (see 47).

### 24. General-purpose ingestor with parent-child chunking
`ingest_text`/`ingest_file`/`ingest_url` and `make ingest` share one chunk → embed → `build_point` pipeline (`app/ingestion/`); every item carries its tenant and is refused without one.
- **Parent and child chunks:** `chunk_text` makes ~1200-character parents and ~600-character overlapping children. Only children are embedded; the payload carries `parent_text`, which the agent prefers. Overlap keeps a boundary from splitting the answering sentence; `_dedupe_by_parent` keeps the best hit per parent.
- **Idempotent ids:** `_content_point_id` derives each point id from `(tenant, source, chunk index, content)`, not `uuid4()`, so re-ingesting identical content upserts instead of duplicating.
- **SSRF guard:** `https` only, every resolved address must be globally routable (`ipaddress.is_global`, an IPv4-mapped IPv6 address judged as the IPv4), no redirects.
- **Bug:** the first guard was a deny-list and missed `100.64.0.0/10` (carrier-grade NAT, where cloud metadata services live). Separately, a blocking `getaddrinfo` in `async def` froze the event loop (a 0.5 s resolver stalled a heartbeat 0.51 s); async callers now use `assert_safe_url_async`.
- **Gap:** validate-then-fetch leaves a narrow DNS-rebinding race, and a hung resolver still holds a task and a thread.

### 25. Input moderation before any spend
`moderate_input` + `route_after_moderation` + `reject_moderation` (`app/agent/moderation.py`) run right after `validate_input`'s checks, before the cache, retrieval or any LLM call. Two layers, cheapest first: (1) a regex screen for known injection/jailbreak phrasings plus a small denylist; (2) an ML classifier, Meta's Llama Prompt Guard 2 (22M) served by `ml-service`'s `/prompt-guard`, which catches paraphrases the patterns miss (`ML_INJECTION_THRESHOLD` 0.5; ~25–100 ms).
- A real hit at either layer fails **closed**; a failure of the check *itself* (a regex bug, `ml-service` unreachable) fails **open**, recorded as `outcome="error"`. The two are deliberately not conflated.
- **Gap:** the threshold is the model's natural boundary, not recalibrated on live traffic. Moderation screens words, not pixels (44).
- **Use when:** any input surface reachable by someone not fully trusted.

### 26. Usage and cost ledger
`record_usage(…)` writes one tenant+principal row per completed turn into `usage_ledger` (`app/agent/usage_ledger.py`), read back by `usage_summary` and the daily cap. Cost is priced per LLM call by `app/agent/pricing.py` from LiteLLM's `GET /model/info` (input, output and cached-input rates), accumulated in `State.total_cost_usd`, and the ledger row stores that same figure.
- **Bug: cost came from a two-entry code table with one rate per total token (spec 008 A2).** Any other alias cost $0 with no signal, and every dollar ceiling multiplies tokens by a price, so pointing `chat` at a paid model blinded the per-turn, per-subagent and per-tenant ceilings at once. Prices now come from LiteLLM, where the alias is already resolved, so swapping the model behind an alias re-prices it and a self-hosted model is priced by `model_info` in the LiteLLM config. **Unknown is not free**: LiteLLM's known $0 (Ollama) is a price; a missing one is `None`, counted per call (`agent_unpriced_usage_total`, alert `ModelUsageUnpriced`) and refused outright under `UNPRICED_MODEL_POLICY=block`. An alias served by several deployments is priced at the dearest, and as unknown if any is unpriced. A delegated run is priced by its specialist's own alias (`GraphDeps.model_alias`).
- **Degrade paths are counted, and two are alerted (spec 008 A1).** A failed ledger write, ledger read, reservation write/release/read or model resolution each increments `agent_cost_governance_degraded_total{path}`. Two hide committed spend, so they page: `LedgerWriteFailing` (the turn succeeded but its row is gone, so every allowance read from that ledger undercounts) and `TenantAllowanceUnenforced` (the allowance read failed open, so those turns ran with no daily cap).
- **The rolling-window read is indexed and the table is trimmed (spec 008 A3).** `17-usage-ledger-indexes.sql` adds `(tenant, recorded_at)` and `(tenant, principal, recorded_at)` (measured on 600,000 rows: 5,770 buffers / 9.3 ms down to 1,857 / 1.5 ms) and drops the now-redundant `(tenant, principal)`. `scripts/usage_ledger_sweep.py` deletes rows past `USAGE_LEDGER_RETENTION_DAYS` in batches (at most 1,000 batches, 5 million rows, per run; hitting that is logged and printed, and the next run continues, so one run's duration is bounded even if something keeps re-inserting old rows); the setting has a 35-day floor, enforced again inside `sweep_old_rows`, so a calendar-month window can never lose spend still inside it. Nothing schedules the sweep. Existing volumes need the index file applied by hand (plain `psql -f`: it uses `CONCURRENTLY`).
- **Resume re-checks the allowance; crash-continue holds a reservation (spec 008 A6).** Only a brand-new turn used to be checked, so a tenant past its ceiling could keep spending by approving paused turns (the approval wait is unbounded) — each resume runs the approved tool and calls the model again. `astream_events_resume` now refuses an over-budget tenant (`tenant_budget_exceeded`; the thread stays paused, and `cancel_run` is never gated because it gives the model no turn) and takes a hold for its duration. The unattended decline loop passes `admitted=True`: it is a step of a request that already passed, and refusing it would strand the conversation at its pause. `astream_events_continue_turn` is **not** refused — it retries admitted work, and refusing could strand a turn that already ran a mutating tool — but it takes a hold, since the crashed worker's was lost. The overshoot that allows is bounded by that one turn's `MAX_COST_USD_PER_TURN`.
- **The allowance's failure mode is a decision, and the rule is its own module.** The check moved out of `runtime.py` into `app/agent/budgets.py` (`check_tenant_daily` returns an `Allowance`, not a bool; `runtime._allowance_refusal` is the one question every entry point asks). When the ledger READ fails, `BUDGET_CHECK_FAILURE_POLICY=open` (default, unchanged) serves the turn, counted and alerted, with `Allowance.degraded=True`; `closed` refuses it as `budget_check_unavailable`, which blames no budget and so doesn't trip the budget alerts. The in-flight hold read stays open either way, since it only closes a race and the ledger check beneath it is still enforced. A refusal is now logged with its tenant and principal (the counter deliberately has no label for them).
- **Bug: unfinished turns weren't recorded.** Only the success branch recorded usage, yet completed steps sit in the checkpoint and timed-out turns spend the most. `_record_unfinished_turn` now reads the last checkpoint on the timeout, error and cancel paths.
- **The daily cap counts in-flight turns.** `reserve_budget` records a hold of `MAX_COST_USD_PER_TURN` per running turn (`tenant_budget_holds`), released by id at the end; holds older than `RESERVATION_STALE_AFTER_MINUTES` stop counting. Without holds, N concurrent turns all read the same "spent so far".
- **Bug: the first design was one running total per tenant.** A killed worker's amount was never released, and every reserve refreshed the same timestamp, so a leaked amount never aged out (still counted 40 minutes later), refusing real turns over spend that never happened. One timestamp can't mean both "last touched" and "how old is what this holds". Fake-cursor tests can't show this; `test_budget_holds_real_postgres.py` ages real rows.
- **Gap:** tokens from a call cut off mid-flight, and turns paused and never resumed, aren't counted. A LiteLLM fallback (`chat` to `chat-backup`) can serve a call at another deployment's price without the response saying so, and cache-WRITE tokens are billed at the input rate because `langchain-openai` drops that field. The cap fails open (a failed reserve means unprotected, not blocked). Existing volumes need `16-tenant-budget-holds.sql` applied by hand.

### 27. Clarification and follow-ups without forking the graph
`ask_clarification` is an ordinary `read_only` tool: its result becomes a `ToolMessage` and `SYSTEM_PROMPT` says to relay it verbatim. Follow-ups are a real node, `suggest_followups`, after `check_output`'s non-retry branch, making one small LLM call for 2–3 questions.
- Follow-ups gate on `used_citations`: an answer with nothing derived (a refusal, a clarification, general knowledge) has none, so one signal suppresses all three cases. They are skipped on a cache hit, since an unconditional call would reintroduce an LLM call the hit exists to avoid.

### 28. Consuming a remote MCP tool catalog
`load_remote_tools(command, args, capability_overrides)` (`app/mcp/client.py`) connects to an MCP server over stdio and wraps each tool as a `StructuredTool` with the remote's JSON Schema passed through.
- **`capability_overrides` is the only source of truth.** MCP `ToolAnnotations` are self-reported hints; a tool not named defaults to `outward`, the same fail-closed default as an undeclared local tool, and the mandatory gate then applies with no special-casing.
- Sync (bridged with `asyncio.run()`) **and** async (for `astream_events`, already inside a loop) by necessity. One fresh connection per call: simpler, at the cost of latency.

### 29. Built-in web UI
One self-contained HTML file (`app/api/static/index.html`, inline CSS/JS, no build step, no CDN) served at `GET /`, talking only to `POST /chat/stream/queued` and rendering the published SSE vocabulary (`token`, `tool_start`, `tool_end`, `citations`, `approval_required`, `error`, `done`). It uses `fetch()`, not `EventSource`, because the endpoint is POST and needs the identity headers `EventSource` can't send.
- **Bug it surfaced:** running the page against a live `uvicorn` raised `asyncio.InvalidStateError` from sync checkpointer calls (`update_state`/`get_state`) on the loop the async checkpointer was opened on. Three call sites needed `aupdate_state`/`aget_state` and a new `resumability_error_async`; the `MemorySaver` suite structurally couldn't catch it.

### 30. Canonical error envelope
One shape, `{code, message, details}`, from a single `ErrorCode` enum (`app/core/errors.py`) for every SSE `error` event and CLI error. It isn't applied to `ToolMessage` content, which is prose for the model.
- `internal_error_envelope(exc)` returns a fixed message plus `error_class`, never `str(exc)`; the text goes to the trace.
- **Bug:** both catch-alls (`runtime_stream.py::_run_graph_stream`, `agent_worker.py::process_request`) sent `str(exc)` to the caller, leaking internal hosts, SQL fragments or DSNs; the worker's had no `code` either. Three tests asserted the leak. They now assert a hostname-shaped sentinel never appears.
- **Gap:** the ingest worker's `process_job` has the same catch-all but mixes uploader-facing messages with unexpected failures, so it needs a per-exception decision; `CrawlFailed`/`IngestRefused` also embed a raw `{exc}`. Read from code, not reproduced.

### 31. DB connection pool
One lazy `psycopg_pool.ConnectionPool` singleton (`min_size=1`, `max_size=10`) behind `get_connection()` in `app/agent/sql_store.py`, so call sites didn't change. `close_pool()` runs in the API lifespan shutdown; without it, background pool threads outlived the process (a reproduced bug).

### 32. Credential scrubbing on tool output
`app/core/scrubbing.py`, at the one chokepoint every tool result funnels through: static patterns for common credential shapes (API keys, AWS ids, `password=`/`token=` pairs, `user:password@`, JWTs) plus this deployment's own configured secret values, so an exact echo of a real secret is caught. Tool output specifically because it never passes through the model's input: a raw DB row can carry a credential straight into `ToolMessage.content`. Fails open (logged) on its own internal failure.

### 33. Memory deletion audit, age selector, retention at recall
`delete_memories(ctx, *, memory_id=None, older_than_days=None, target_principal=None)` requires **exactly one** selector, refusing an ambiguous request rather than narrowing to "everything". It counts matches with the same filter before deleting, and records every call, refused or not, via metrics and a log. Independently, `Policy.lower`'s memory filter ANDs a `DatetimeRange(gte=cutoff)` (`MEMORY_RETENTION_DAYS`, 365) onto every recall, so an unswept expired memory is invisible regardless.
- **Gap:** nothing calls `delete_memories` (no script, endpoint or make target); retention is enforced at read time only.

### 34. No-progress detection
`_consecutive_repeat_count` scans backward from the latest `HumanMessage`; `_tool_call_fingerprint` normalizes a batch (tool name + sorted args) order-independently. `MAX_REPEATED_ACTIONS` (3) identical consecutive batches ends the turn, usually before `MAX_ITERATIONS`. It is a pure function of `state["messages"]`: the history already is the record, so there is no second counter to drift.

### 35. Cost ceiling
`MAX_COST_USD_PER_TURN` is a hard stop on cumulative per-turn dollars, distinct from the token cap because the same tokens cost differently by model tier. `agent()` computes incremental cost per call from LiteLLM's input/output/cached rates (`pricing.price_usage`), the same running total the ledger row is written from. It is checked **before** the next call: enforcement, not just recording.

### 36. Run cancellation
A third outcome for a paused interrupt, not a repurposed rejection: `route_after_approval` checks `cancelled` first and goes to `__end__`, never back to `agent` (`CANCEL_SENTINEL`, `runtime_stream.py::cancel_run`). A rejection lets the agent react; a cancel must stop the run.
- **Bug: a cancel outlived its run.** Nothing cleared `cancelled`, so a later pause on the same thread that the person *approved* went straight to `__end__`, leaving a dangling `tool_call`. `validate_input` now resets `cancelled`/`approved` each turn (a resume skips it). Test: `TestApprovalAfterAnEarlierCancel`.
- **Bug: an approved run couldn't be cancelled.** `astream_events_resume` took no `cancel_check`, so `POST /chat/cancel` between "approve" and "done" set a flag nothing polled. It now forwards it: a cancel before the worker picks the job up stops the turn before the approved write, and a later one stops it at the next event boundary (a running tool finishes; cancellation is cooperative).
- **Use when:** a human might change their mind mid-review.

### 37. Per-tool-call audit record
`MetricsCallbackHandler` correlates a `tool_called` start log with its success/failure log by LangChain's `run_id`. Args and results are logged as a truncated SHA-256 fingerprint, never raw content (pattern 14's rule extended to tools). "Which tool ran, with what, did it succeed" is answerable from logs at 3 a.m.

### 38. Resolved model recording
`model_resolver.resolve_model` asks LiteLLM's `GET /model/info` which concrete model serves an alias like `chat`, because neither the response body nor LangChain metadata carries it. Cached per process, degrades to `None`, recorded as `resolved_model` in the ledger. A silent gateway remap of the model behind an alias is otherwise invisible.
- **Bug:** the first version was a synchronous `httpx.get` on the event loop at the end of every turn. A slow LiteLLM froze every other turn and health check on that worker (a 0.5 s answer stalled a heartbeat for 0.56 s), and during an outage every turn paid a fresh 5 s timeout. It is now `async` and remembers a failure for `FAILED_LOOKUP_RETRY_SECONDS` (60) per alias. The regression test runs the real resolver against a deliberately slow local server with a heartbeat, which a mock can't do. `tests/conftest.py::mock_model_resolver` (autouse) keeps the default suite off the network.
- **Gap:** concurrent first lookups during an outage can each make one request before the failure is remembered.

### 39. Ungrounded-claims count
`_ungrounded_claims_count` in `check_output` mirrors `_used_citations` (20) but is computed **independently** from the same answer text, so a bug in one can't mask a bug in the other. It counts `[n]` markers matching no returned citation, which makes "did the model cite something real" a measurable, gateable property (40).

### 40. Eval statistical rigor
`scripts/eval.py` has two release gates. (1) Each case runs `EVAL_REPETITIONS` (5) times and passes only if `REPETITION_PASS_THRESHOLD` (80%) of repetitions pass: a stochastic system graded once is a coin flip. (2) A grounded-claims gate over the whole golden set (≥95% of citation markers real), computed from the runtime's own `used_citations`/`ungrounded_claims_count`, never a model's opinion of itself. Tokens, cost and grounding are summed across repetitions; latency is averaged. The thresholds are demo defaults; the structure generalizes.

### 41. Bounded history summarization
`compact_history` summarizes exactly the discarded messages with one LLM call, folding the result into a cumulative `state["history_summary"]` that is never reset per turn. `agent()` front-loads the summary at the same anchor retrieved context uses, with a short "don't restate this" reminder at the tail (a small model was seen regurgitating the injected summary). A separate field, because `add_messages` has no insert-at-position.
- If the summary itself passes `MAX_HISTORY_SUMMARY_CHARS`, `route_after_compaction` routes to `context_window_exceeded`: a named dead end, not silent truncation. A summarization failure still applies the trim and skips updating the summary.

### 42. Telegram channel
`app/channels/telegram.py` long-polls `getUpdates`, resolves a stable `thread_id`/`SecurityCtx` per chat, and drives `astream_events_turn_unattended()`, collecting the stream into one reply. Long-polling needs no inbound port or public URL; production would use `setWebhook`.
- It reaches the public internet by design. `TELEGRAM_BOT_TOKEN` is empty by default and `run()` refuses to start without one.
- **Approvals reuse the auto-decline** (no approve/reject UX): a gated call is declined up to `UNATTENDED_MAX_DECLINE_ROUNDS` times, then the run is cancelled (pattern 8) with a real reply explaining why, never a silent write. `_send_message` sends nothing for an empty string, so `handle_message` substitutes `_NO_REPLY_FALLBACK` when a turn produced no text; before that, an empty turn read as the bot ignoring the user.
- **Bug: the long-poll `offset` was a local variable.** A restart reset it to 0 and Telegram redelivered every update it still remembered, each producing a duplicate turn and reply to a real user. It is now durable in Redis (`telegram:offset:{domain}`) and persisted **after** handling, so a mid-handling crash means one duplicate reply, never a dropped message.
- **Gap:** one thread is still shared across every user of a group chat (17).

### 43. Redis Streams queue between the API and agent workers
The only HTTP chat path. `POST /chat/stream/queued` publishes a turn to a Redis stream and relays events from a per-request results stream as SSE; it never runs the graph. `agent_worker.py` pulls through a consumer group, so each request reaches one worker and **N workers is the scaling story**. The SSE tier scales connections and the worker tier scales turns, independently.
- Results streams expire (`RESULTS_STREAM_TTL_SECONDS`, 300), so one turn's events can't reach another's response.
- **At-least-once:** a request is ACKed only after `process_request` finishes; a crashed worker's job is reclaimed (see Extending Further).
- **Bug:** redis-py's async `socket_timeout` defaults to 5 s, racing `XREAD`'s server-side `BLOCK`, so blocking reads timed out early. Fixed with `socket_timeout=None` plus a regression test.
- **An answerless pool must be visible.** With no worker running, each request got one error and nothing could page anyone. `read_results` now counts a first-event timeout (`agent_worker_unreachable_total{queue}`, alert `WorkerUnreachable`); a slow job that was picked up doesn't count. `test_alert_rules.py` checks every metric an alert names is defined, since an alert on an unrecorded metric is silent forever.
- **Not done:** a consumer-lag gauge, a readiness probe for consumers, a "no requests at all" rule. `promtool test rules` isn't wired in.
- **Use when:** SSE connection count and LLM concurrency need to scale on different axes.

### 44. Multimodal image questions
`_build_human_content(text, images)` returns a plain string (byte-identical to before) with no image and an OpenAI/LiteLLM content list with one or more. The app never fetches or decodes an image; the backing model does. Five call sites (`route_after_validation`, `moderate_input`, `check_semantic_cache`, `retrieve_context`, `write_semantic_cache`) assumed `.content` was a string, so `_human_text`/`_human_has_content` handle both, and an image-only question isn't rejected as empty.
- **Two scope boundaries:** moderation screens words, not pixels (an image-only message passes); retrieval and caching stay text-only. Verify vision **and** tool-calling support before assuming an alias swap is enough.

### 45. Skill packages with progressive disclosure
A catalog of `SKILL.md` packages (YAML frontmatter + markdown, one directory under `skills/`) discovered by meaning. `skill_search` hybrid-searches `{name, description}`; `use_skill(name)` loads one matched skill's full body. A turn that needs no skill pays for two tool schemas, not the size of the catalog (`app/agent/skills.py`, `scripts/index_skills.py`).
- Reuses `hybrid_search` via an optional `collection` parameter; no forked fusion logic. **Disk is content truth, Qdrant only the index:** `use_skill` reads the body from an on-disk registry, so the index can't disagree with the file.
- Skills are a shared bundled catalog (no `SecurityCtx`, like `calculator`); a tenant-authored catalog is a larger extension not attempted. Domains scope skills with an optional `domains:` field.

### 46. Subagents: scoped, isolated delegation
`run_subagent(subagent_name, task)` starts a separate nested run: a fresh `build_graph()` with its own messages, prompt and budget. Skills add instructions to the same context; this doesn't. One tool with a closed enum built from `subagents/<name>/AGENT.md` (`app/agent/subagents.py`).
- **Read-only tools only,** enforced at catalog-build time (others are dropped with a warning), so `run_subagent` is a plain `read_only` tool needing no gate special case. The nested graph runs synchronously on a throwaway `MemorySaver` inside one tool call; nobody could resume it hours later, so a write-capable subagent would mean a deadlocked gate or a bypass of pattern 15.
- **No recursion:** `run_subagent` is stripped from every subagent's tools.
- **Isolation:** fresh messages (its own prompt, the task as the only `HumanMessage`), inherited `SecurityCtx`, its own budget (`MAX_SUBAGENT_ITERATIONS=6`, `MAX_SUBAGENT_TOKENS_PER_RUN=4000`) via pattern 23's `functools.partial`, and a leaner `build_subagent_graph()` without 5 of 21 nodes (cache, follow-ups, compaction). Graphs are cached by `(domain, subagent_name)`.
- **Spend:** nested spend folds into the parent's budget through the `subagent_spend` reducer `_concat_or_reset`, which concatenates and treats `None` as a reset (pattern 10's bug). Parent callbacks thread into the nested run, and its reasoning tokens never reach the client's stream.
- **Gap:** a new subagent needs a process restart.

### 47. Example domains as a load-bearing proof
`app/domains/support/`, `ops/` and `sales/` turn pattern 23's test-only proof into three runnable products on the same unmodified graph: a Tier-1 support copilot, an internal ops bot (reusing this repo's Prometheus thresholds) and a sales/CRM concierge. Each follows `sql_store.py`/`tools.py` conventions, plus a shared `app/domains/policy.py::ActionAllowlistPolicy`.
- **Runtime change kept minimal:** `init_graph_async` takes an optional `manifest`/`domain` (default `None`, today's exact behavior): which one domain this process boots as, decided at start from `AGENT_DOMAIN`. Not a multi-domain registry.
- **An unattended cron job can never approve itself.** `post_to_team_channel` is the first real `outward` tool and the gate has no bypass flag; auto-declining would silently make a digest never post. `scripts/ops_digest.py` and `followup_sweep.py` call the domain's `_impl` functions directly: a fixed pipeline that never enters the tool-calling loop.

### 48. Real-backend testing at every layer
`tests/containers.py` provides `ensure_*()` helpers (Postgres, Redis, Qdrant, Ollama, …) that start ephemeral testcontainers, so `tests/integration/` and `tests/live/` run in CI while a no-Docker `pytest -q` stays fast. `promptfoo/` and `garak/` target the same small Ollama model.
- **xdist:** shared containers need a fixed cache dir, not pytest's rotating `basetemp`; Ryuk is off and the controller tears down in `pytest_sessionfinish`. Concurrent first `init_graph_async()` calls hit an upstream `UniqueViolation` in `AsyncPostgresSaver.setup()`, so setup runs once, under a lock.
- **Tool calling is real:** `OPENAI_API_BASE` points at Ollama's OpenAI-compatible endpoint, confirmed by a raw `curl` to return structured `tool_calls` (LiteLLM's `ollama/` provider fakes them).
- **Tool gotchas:** promptfoo `file://` paths resolve from the config file, not the CWD; garak's `uri` must nest under `plugins:` or it is silently ignored.
- **Small local models make poor judges and targets.** A garak `dan` probe had 100% attack success on `qwen2.5:1.5b`, so `moderation.py` is the real defence. A local-judge redteam run reported 93% failed from malformed prompts and a grader inventing evidence, and deepeval's metrics couldn't separate good answers from bad ones either. So the judge is hosted (Gemini, Groq) and the target stays local.
- **Redteam findings, fixed:** a system-prompt leak under an "act as an auditor" framing, and a destructive script written for "wipe temp files" under urgency. Both got explicit prompt rules and were re-verified. Narrower paraphrase gaps remain: a ceiling of prompt-only defence on a small model.
- **deepeval** (`make deepeval`) checks faithfulness and relevancy to cited context, tool and argument correctness, and multi-turn role adherence and knowledge retention. In CI every case is `flaky=True`, so only a crash fails the job.
  - **Judge failover** (`tests/deepeval/fallback.py`): the primary, then other models on the same provider, then optional Plugsky and OpenRouter free models. It hands over on a rate limit, overload or timeout (and skips that model for 5 minutes, an hour for a daily limit), and on an unusable answer: invalid JSON or the wrong shape, for that one call, with no cooldown.
  - **Bug:** the first version never fired. deepeval's tenacity policy has `reraise=False`, so the test sees `RetryError` wrapping the real `RateLimitError`, and the check read only the outer exception. It now looks inside and logs model, class and status only (the old log leaked an org id and quota figures into public logs).
  - **Bug: a judge's formatting turned the job red.** `main` went red twice running (#120, #121) with the judge up but its answer unusable: `{"verdicts": {...}}` where a list was wanted (pydantic `ValidationError`) and an empty completion (`DeepEvalError: invalid JSON`). `flaky=True` swallows a failed score, not an exception, and the chain re-raised both as "not transient". deepeval's `LocalModel` sends no `response_format`, so the model is only asked for JSON in the prompt. The chain now hands those to the next model too, found by class name through `__context__` (deepeval leaves the `JSONDecodeError` only there), and logs the model and class, never the answer. Reproduced against real `LocalModel`s and a stub server: the old chain raised both errors, the new one recovers. If every model answers badly it still raises.
  - **Gap:** free-tier limits are per plan or account, not per model, so more models in one provider add no quota; OpenRouter's 50/day can run out in one run.
  - **Gap:** which model gave the bad answers in #120 and #121 is unproven (the log only shows the Groq primary out of daily tokens, so a weaker fallback is the likely source). Whether a `response_format` passed through `generation_kwargs` would stop them at the source is untested against live Groq.
- **Bug: a session-scoped stack leaked state.** A memory saved by the approve-button test ("dark roast coffee") was pre-fetched into the skill test's turn; the 3B model then called `skill_search` with no arguments and proposed `add_note`, pausing for an approval nobody could give. It failed in ~40 CI runs and passed alone. An autouse fixture now deletes each test's memories (`tests/live/memories.py`).
- **Wait for the turn to end, not the text.** `to_contain_text(timeout=<whole budget>)` kept polling after the agent had stopped, wasting about half the job. Tests now wait for the page's "turn over" signal, and a failed browser test prints the page transcript (`tests/live/transcript.py`): messages, tool calls with arguments, the approval box.
- **The subagent flake.** The transcript showed the parent paraphrases the task into `run_subagent`, and the nested run calls `query_employees(name_contains="Support Lead")`, which finds nothing because that's a title. Both fixes tried made things worse (a prompt tweak: 3 of 3 failures against 4 of 4 passing; a `title_contains` filter fixed the explicit prompt but broke the repo's own, 4 of 4 → 0 of 3), so neither shipped. A 3B model's tool use shifts with any wording, and local pass rates don't predict CI. A rerun was rejected: the semantic cache would replay the wrong answer.
- **A gate must fail for a reason a change can cause.** Two e2e tests (the skill chain, subagent delegation) fail unpredictably: one fully green run in five. They carry `@pytest.mark.advisory` and run in their own `continue-on-error` step, while `-m "e2e and not advisory"` is the gate; `tests/core/test_live_e2e_gate_split.py` pins the split. **Trade-off:** a regression that breaks only skills or subagents no longer turns the job red; it shows in that step's log, which someone has to read.

### 49. Per-domain requests streams
Every queued job once went onto one `agent:requests` stream, so the web UI reached only whichever domain a worker pool happened to run. `requests_stream_key(domain)` (`agent:requests:{domain}`) gives each domain its own stream and consumer group; `get_domain` reads `X-Domain` (default `ecorp`), so one API process reaches every domain with a running pool.
- The domain is inferred from which stream a message landed on, never carried in the payload: a resume or cancel must land where the original turn did, since the paused graph was compiled from that domain's manifest. An unknown `X-Domain` is a 422 rather than a publish nothing reads, which would hang silently.

### 50. Web crawling (crawl4ai) and a sandbox (OpenSandbox over MCP)
crawl4ai (`app/ingestion/web_crawler.py`) renders JS-heavy pages. OpenSandbox, consumed over MCP through pattern 28's unmodified `load_remote_tools`, runs code in isolation. Both go through the shared SSRF guard (`app/core/url_safety.py`), and every tool is `outward`, so the approval gate always fires.
- **Wrapped narrow:** handing a 3B model OpenSandbox's ~19 tools made it hallucinate a `sandbox_id`, and three prompt fixes didn't help. `app/domains/sandbox_session.py` now does lookup and creation in code and exposes four flat tools (`run_command_in_sandbox`, `run_python_in_sandbox`, `read_sandbox_file`, `write_sandbox_file`). `run_python_in_sandbox` takes the script as a plain argument, not a shell string, because the model kept breaking `python -c '...'` quoting.
- **Check the structure before blaming the model:** a skill-based math question never reached `skill_search` despite three prompt fixes. The cause was list position (those tools were last); moving them first fixed every domain.
- **`check_output` gained two retry reasons:** `fabricated_tool_output` (an invented tool result with no real `tool_calls`; ranked first) and `skipped_required_tool`.
- **Bug:** two simultaneous tool calls sometimes arrived glued into one `tool_calls` entry. Fixed with `parallel_tool_calls=False` on `bind_tools()`.
- **Bug: tenants could share a sandbox.** Telegram ids (`telegram:123`) broke sandbox metadata (`:` is forbidden), and the first fix rewrote the id many-to-one without naming the tenant, so two tenants' conversations could share a sandbox and read each other's files (Principle I). Now tenant and raw conversation id are tagged as separate SHA-256 prefixes, lookups filter on both and re-check returned tags, and `tenant` is a required keyword argument on every sandbox helper. Old-tag sandboxes expire on their TTL.
- **Misdiagnoses traced to the real request:** a 405 was a port clash with open-webui (both on 8080); a timeout was the SDK reaching a Docker-internal IP, fixed with `use_server_proxy=True` in `scripts/opensandbox_mcp_bridge.py`.
- **Third-party bugs, documented:** `read_sandbox_file` 404s right after a write (use `cat` via `run_command_in_sandbox`); a non-root image needs an explicit `WORKDIR`.
- **Not verified against a real service:** that the sandbox honours the metadata filter, and its "no network egress" claim (spec 009, A2). The catalog itself isn't tenant-scoped: it's a shared resource, and the approval gate is the boundary.

## Graph Flow

```
START
  ↓
validate_input  ← resets iterations/total_tokens/run_id/cancelled/approved,
  ↓                stamps SecurityCtx from config["configurable"]["ctx"]
route_after_validation?  ← valid ctx? last HumanMessage non-empty?
  ├─→ reject_context  (no valid tenant+principal — checked FIRST, pattern 17) → END
  ├─→ reject_input    (AIMessage explaining the problem) → END
  └─→ compact_history  ← trims past HISTORY_TOKEN_CEILING down to FLOOR and folds the
       │                  dropped turns into history_summary (pattern 41); no-op in budget
       ↓
      route_after_compaction?  ← history_summary over MAX_HISTORY_SUMMARY_CHARS?
       ├─→ context_window_exceeded  (a named dead end, not silent truncation) → END
       └─→ moderate_input  ← regex screen + Prompt Guard classifier (pattern 25);
            │                 fails closed on a hit, open on the check's own failure
            ↓
           route_after_moderation?
            ├─→ reject_moderation ("I can't help with that request.") → END
            └─→ check_semantic_cache  ← tenant+principal cosine-KNN in Redis (pattern 22);
                 │                       degrades to a miss on any failure
                 ↓
                route_after_cache?
                 ├─→ check_output   (HIT: cached answer as a final AIMessage, no retrieval, no LLM)
                 └─→ retrieve_context  ← MISS: hybrid dense+BM25, RRF, rerank, numbered citations
                      │                  (pattern 20) + this principal's memories; degrades on failure
                      ↓
                     agent  ← LLM call with context as an untrusted <retrieved_document>
                     │        SystemMessage; retried on transient failure (AGENT_RETRY_POLICY);
                     │        may call ask_clarification (pattern 27)
                      ↓
                     should_continue?
                      ├─→ too_many_tool_calls  (> MAX_TOOL_CALLS_PER_TURN) → agent, with ToolMessage rejections
                      ├─→ human_approval  (require_approval, OR any non-read_only call — mandatory, pattern 15)
                      │    ↓
                      │   route_after_approval?  ← interrupt() paused here
                      │    ├─→ tools      (approved)
                      │    ├─→ agent      (rejected, with synthesized ToolMessage rejections)
                      │    └─→ __end__    (cancelled — checked FIRST, never loops back, pattern 36)
                      ├─→ tools  ← concurrent execution; may include remote MCP tools (pattern 28)
                      │    ↓
                      │    agent  (loop)
                      └─→ check_output  ← cache hits and misses both rejoin here; computes
                           │               used_citations and ungrounded_claims_count (20, 39)
                           ↓
                          route_after_check?  ← answer too short, fabricated, or skipped a required tool?
                           ├─→ retry_output  ← corrective HumanMessage → agent
                           └─→ suggest_followups  ← only for a GROUNDED answer (used_citations
                                │                     non-empty); skipped on a hit (pattern 27)
                                ↓
                               write_semantic_cache  ← writes back unless this turn was a hit
                                ↓                       or called a non-read_only tool (pattern 22)
                               END
```

`should_continue` also ends the run early (→ `__end__`, skipping `check_output`) on the iteration cap,
`MAX_TOKENS_PER_TURN`, `MAX_COST_USD_PER_TURN` (35) or `MAX_REPEATED_ACTIONS` identical batches (34).
They aren't drawn as branches, but each is an independently tested exit.

**21 nodes:** `validate_input`, `reject_input`, `reject_context`, `compact_history`,
`context_window_exceeded`, `moderate_input`, `reject_moderation`, `check_semantic_cache`,
`retrieve_context`, `agent`, `tools`, `human_approval`, `too_many_tool_calls`, `invalid_tool_call`,
`use_skill_without_search`, `check_output`, `retry_output`, `retry_exhausted`, `no_answer`,
`suggest_followups`, `write_semantic_cache`.

**State fields beyond `messages`:** `context`, `citations`, `used_citations`, `ungrounded_claims_count`,
`cache_hit`, `moderation_blocked`, `followups`, `history_summary`, `iterations`, `total_tokens`,
`total_cost_usd`, `run_id`, `graph_version`, `state_schema_version`, `ctx`, `require_approval`,
`approved`, `cancelled`, `subagent_spend`.

## Extending Further

### Exactly-once side effects: how the gaps closed

Two kinds of duplicate: a **replay** sees the same `tool_call_id` twice; a **re-ask** arrives under a new
`tool_call_id`, which nothing keyed on the id can recognize. The layering is summarized in the
[README](README.md#safety-model); each row here is a defect found and closed.

| Gap | Fix |
|---|---|
| A tool call runs twice (a reclaimed `resume`) | `tool_idempotency.py::idempotent` wraps every `mutating`/`outward` tool, keyed by `tool_call_id` in `tool_call_dedup` (`13-`). A second call returns the cached result. Resuming re-invokes pending calls under their **original** ids, so reclaiming a `resume` is always safe. Fails open (`agent_tool_dedup_degraded_total`) |
| A worker dies mid-turn; a restart would re-ask the LLM for new ids | `queue.py::reclaim_stale_entries` (`XAUTOCLAIM`) runs in both workers. `_classify_reclaimed_turn` reads the checkpoint (never re-running anything): no `HumanMessage` yet → rerun; own `HumanMessage` checkpointed and unfinished → **continue** the run (`astream_events_continue_turn`), even after a write, since no new id is minted; finished, approval-paused or unreadable → dead-letter. Retries are capped by `MAX_AUTO_RECLAIM_RETRIES`; `agent_worker_job_reclaimed_total{queue,outcome}` separates `retried` from `dead_lettered` |
| A finished zero-tool turn is reclaimed before its ack | `_turn_already_completed` refuses it; a tool-call check alone re-ran it and double-recorded its cost |
| Ingest jobs on reclaim | Idempotent by construction (`_content_point_id`, pattern 24), so retried unconditionally |
| A client resubmits the same message | `claim_or_get_existing_submission` reuses the first attempt's `request_id` within `CHAT_SUBMIT_DEDUP_TTL_SECONDS`. This exposed a bug: results streams were deleted when any reader saw the terminal event, unsafe once two readers share one, so they now rely on the TTL. `release_submission_claim` undoes a claim whose publish failed |
| A tool times out but its write lands | `MutatingToolTimedOut` steers the agent to verify with a read-only tool first. `asyncio.wait_for` cancels the awaiting task, not necessarily the write, and "retry" would be a fresh id |
| The dedup claim races (`result IS NULL` → run again) | `add_note`/`remember` point ids are `uuid5` of `tool_call_id`; `create_ticket`/`log_incident`/`add_followup` carry `tool_call_id UNIQUE` + `ON CONFLICT DO NOTHING` (`14-`) |
| An append to a text column has no row for `ON CONFLICT` | `15-` makes each append its own row (`support_ticket_comments`, `crm_lead_notes`); flattened text is computed at read time, so `tools.py` didn't change. `find_or_create_lead`'s upsert is naturally idempotent and needs no key |
| Nobody ever publishes (no worker, or a poisoned dedup claim) | `read_results` bounds the wait for the **first** event only (`first_event_deadline_seconds`), so long ingest jobs aren't cut short |
| One bad upload aborts the batch or orphans a blob | `POST /ingest/upload` returns per-file errors and deletes a blob that failed to publish |
| `tool_call_dedup` grows forever | `scripts/tool_call_dedup_sweep.py` (`make tool-call-dedup-sweep`) |
| `usage_ledger` grows forever | `scripts/usage_ledger_sweep.py` (`make usage-ledger-sweep`) |
| Telegram redelivers after a restart | Durable offset in Redis (pattern 42) |

**Deliberately not built:** a business-key rule ("one open ticket per tenant + requester + subject") for two
*different* ids used for the same request. What counts as "the same ticket" is a product decision;
`MutatingToolTimedOut`'s steering is the general mitigation until a domain needs one.

### Not built

- **A fallback node** for the primary LLM path.
- **A vision model that also does tool calling** (44). Small local vision models do one or the other; the `vision` alias is a slot, not a verified default.
- **Image-aware moderation, and Telegram/CLI image input.**
- **Crash-restart and autoscaling for workers.** `--scale` works and workers handle `SIGTERM`, but nothing restarts a crashed one or scales on queue depth.
- **A Telegram webhook.** Long-polling needs no inbound port; production would use `setWebhook`.
- **Real authentication.** Pattern 17's `SecurityCtx`/`Policy` is the isolation structure; header extraction isn't authentication.
- **Per-action authorization within a tenant.** Every principal has the write capability the gate allows at all; a `Policy` reading `ctx["claims"]` would live here.
- **A multi-domain runtime** (partly done, pattern 49). The API routes per request via `X-Domain`, but one process still builds one graph. Serving several would need a per-domain graph registry and a seeding cache keyed by `(domain, thread_id)`.

### Known gaps

From reviewing the as-built system against the constitution; each notes how it was established, and a fix PR
deletes it from this list.

- **The shipped proxy does not authenticate** (*read from `deploy/caddy/Caddyfile`*). It neither sets nor strips `X-Tenant-Id`/`X-Principal-Id`, so a caller can name any tenant. Isolation then guards against bugs, not against a caller who sets another tenant's header. Put an authenticating gateway in front.
- **The ops domain is global** (*read from code*). `ops_incidents` has no tenant dimension by design, any caller can name the domain, and nothing authorizes which tenants may use it. Principle I has no carve-out for it.
- **Approvals are unattributed** (*read from code*). Decisions are counted by outcome only (no approver, time or action), so the gate is enforced but not auditable, and the principal is whatever header the caller set.
- **The `tool_call_dedup` lookup isn't tenant-scoped** (*read from code*). It assumes provider ids are globally unique. Adding a tenant predicate changes what a collision means (a miss ⇒ a second write), so it needs a decision.
- **A residual duplicate window for team-channel posts** (*read from code and the spec*). If a second run finds the first's dedup record still in flight, it runs the call. Row-backed writes are protected by uniqueness; the three tools that also post to a channel (sales handoff, support escalation, ops `post_to_team_channel`) rely on the conversation lock and on reclaim waiting longer than a turn, so a second run can post twice. The write itself is unaffected; the post is best-effort by design (`app/domains/notify.py`).
- **Nothing schedules `make tool-call-dedup-sweep` or `make usage-ledger-sweep`** (*read from code*), so those rows accumulate until someone runs them.
- **Memory deletion has no entry point** (*read from code*). `delete_memories` is built and tested, but nothing calls it; retention is enforced at read time only.
- **Tenant isolation is only partly proven against real backends** (*read from tests*). Documents and the cache's tenant axis hit real Qdrant/Redis; memories, relational stores, sessions and the cache's principal axis rely on hermetic tests.
- **`reclaim_stale_entries` is tested only against a fake** (*read from tests*), hand-written and not paginated like real `XAUTOCLAIM`; no integration test covers it.
- **The answer cache key ignores conversation context** (*read from code, not reproduced*). The key is the last human message, so a follow-up like "be more detailed" can hit an answer cached for a different conversation of the same caller. Bounded by tenant+principal scope and the TTL: wrong context, not a leak.
- **A refused resume emits an error with no `code`** (*read from code*). `astream_events_resume` yields `{"type": "error", "content": "checkpoint_lost: …"}` outside the envelope, so a client can't switch on it (pattern 30).

**The key insight:** LangGraph lets you make every step of the pipeline explicit and controllable. That is
what separates it from "just LLM + tools."
