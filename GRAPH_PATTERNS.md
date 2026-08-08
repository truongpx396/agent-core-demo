# Enhanced LangGraph Patterns

## Why This Matters vs. Basic "LLM + Tools"

This enhanced graph shows **realistic patterns** you'll use in production LangGraph agents:

### 1. **Input Validation with a Real Exit (`validate_input` + `route_after_validation` + `reject_input`)**
- **Pattern**: Guard early, and actually terminate the invalid path instead of falling through.
- **Why**: A validation node that can't stop the graph isn't a guard, it's decoration. The routing decision lives in a separate conditional-edge function (`route_after_validation`), which sends bad input to `reject_input` (a dedicated node that returns an `AIMessage` — the *system* is speaking, not the user) and terminates at `END`. Valid input proceeds to `retrieve_context`.
- **Real-world**: Prevent empty strings, validate format, check user permissions, rate-limiting — with an actual short-circuit.

### 2. **Context Enrichment (Node: `retrieve_context`)**
- **Pattern**: Pre-fetch and prepare context *before* the LLM reasons, and make sure it's actually used.
- **Why**: Gives the LLM relevant facts upfront (RAG pattern). Reduces tool calls and improves accuracy. The `agent` node appends the retrieved context as a `SystemMessage` right before invoking the LLM — the base `SYSTEM_PROMPT` is seeded once per thread by `app/agent.py::_ensure_seeded`, so `agent()` only adds the per-turn context on top rather than duplicating the whole system prompt.
- **Real-world**: Fetch from database, call APIs, load user history, retrieve search results.

### 3. **State Tracking (State fields: `iterations`, `context`)**
- **Pattern**: Make flow control explicit in state.
- **Why**: Enables loop limits, retry logic, conditional routing, debugging.
- **Real-world**: Prevent infinite loops, track usage, make decisions based on accumulated state.

### 4. **Conditional Routing (Edge fn: `should_continue`)**
- **Pattern**: Decide which path to take based on current state. Note this is an *edge function*, not a node — it never appears in the graph's node list, it just decides where execution goes after `agent`.
- **Why**: Not every agent query needs tools. Not every tool call succeeds. Route intelligently.
- **Real-world**: Route math questions to calculator, docs questions to search, simple Q&A directly to LLM.

### 5. **Output Quality Gate with a Real Retry (`check_output` + `route_after_check` + `retry_output`)**
- **Pattern**: Validate the final answer, and actually act on the result — not just pass through to `END` regardless.
- **Why**: `route_after_check` inspects the last message; if it's suspiciously short, it routes to `retry_output`, which appends a corrective `HumanMessage` ("that answer was too short...") and loops back to `agent` for another attempt. Otherwise it ends. `MAX_ITERATIONS` (via `should_continue`) still bounds the total number of retries, so this can't loop forever.
- **Real-world**: Length checks, required information presence, confidence scoring, fact-checking, hallucination detection.

### 6. **Loop Control (via `MAX_ITERATIONS`)**
- **Pattern**: Prevent infinite tool-calling *and* retry loops.
- **Why**: Safety net shared by both the tool loop and the output-retry loop — an agent can get stuck either way.
- **Real-world**: Most production agents limit to 5-15 iterations; if not done by then, something's wrong.

### 7. **Error Recovery (`ToolNode(TOOLS, handle_tool_errors=_friendly_tool_error)`)**
- **Pattern**: A tool exception becomes a `ToolMessage` the agent sees on its next turn, not an unhandled exception that kills the run.
- **Why**: `search_docs` calls out to Qdrant; `calculator` parses arbitrary expressions. Either can throw. `handle_tool_errors` (a `ToolNode` constructor arg) accepts a callable — ours (`_friendly_tool_error`) turns the exception into a short string, so the agent can apologize, fall back to general knowledge, or try a different tool instead of the whole graph crashing.
- **Real-world**: Any tool hitting a network service, a flaky API, or unpredictable user-controlled input.

### 8. **Human-in-the-Loop (`human_approval` + `route_after_approval`, gated by `require_approval`)**
- **Pattern**: Pause the graph before running tool calls and wait for an external decision, using LangGraph's `interrupt()`.
- **Why**: `interrupt()` suspends execution inside `human_approval` and persists state via the checkpointer; a caller resumes with `graph.invoke(Command(resume=True_or_False), config)`, at which point `interrupt()` returns that value and the node continues. It's gated behind a `require_approval` state flag that defaults to `False`, so `app/chat.py` and `app/api.py` are completely unaffected — tool calls still run immediately for them. **See `app/hitl_demo.py` for a runnable, self-contained example** that opts a thread into approval and drives the pause/resume loop.
- **Gotcha this demonstrates**: if approval is rejected, you can't just skip straight to `agent` — every pending `tool_call` needs a matching `ToolMessage` response or the next LLM call fails (OpenAI's API requires it). `human_approval` synthesizes a rejection `ToolMessage` per pending call before routing back to `agent`.
- **Real-world**: Approval gates before sending an email, executing a database write, spending money, or any other side-effecting tool call.

### 9. **Parallel Tool Execution (built into `ToolNode`, no extra code)**
- **Pattern**: When one LLM turn requests multiple tool calls (e.g. "search the docs AND compute 12*7"), `ToolNode` already runs them concurrently — there's no separate "parallel" node to add.
- **Why worth knowing**: it's easy to assume you need custom fan-out/fan-in wiring for this; you don't, for the common case of "multiple tool calls in one AI turn." (LangGraph's `Send` API exists for the different case of fanning a single node out over a dynamic list of *graph* branches, which this example doesn't need.)
- **Real-world**: Any query that naturally needs two independent lookups — the model just needs to be capable of emitting multiple `tool_calls` in one message, which most modern tool-calling models do by default.

## Graph Flow

```
START
  ↓
validate_input
  ↓
route_after_validation?  ← Decide: is the last HumanMessage non-empty?
  ├─→ reject_input  ← AIMessage explaining the problem
  │    ↓
  │   END
  │
  └─→ retrieve_context  ← Enrich: fetch relevant docs
       ↓
      agent  ← Think: call LLM with context injected as SystemMessage
       ↓
      should_continue?  ← Decide: tools? approval needed? done? max iterations?
       ├─→ human_approval  (only if require_approval=True on input state)
       │    ↓
       │   route_after_approval?  ← interrupt() paused here for a decision
       │    ├─→ tools     (approved)
       │    └─→ agent     (rejected — with synthesized ToolMessage rejections)
       │
       ├─→ tools  ← Execute tool calls (concurrently, if there are several)
       │    ↓
       │    agent  ← Loop back to think again
       │
       └─→ check_output
            ↓
           route_after_check?  ← Decide: is the answer too short?
            ├─→ retry_output  ← Append corrective HumanMessage
            │    ↓
            │    agent  ← Loop back with feedback
            │
            └─→ END
```

## Differences from Basic Agent

| Aspect | Basic | Enhanced |
|--------|-------|----------|
| **State** | Just messages | Messages + context + iterations + require_approval + approved |
| **Nodes** | agent + tools | 9 nodes: validate_input, reject_input, retrieve_context, agent, tools, human_approval, check_output, retry_output |
| **Flow** | LLM ↔ tools loop | Multi-stage pipeline with three real conditional gates |
| **Context** | LLM decides what to search | Pre-fetched, and actually injected into the LLM call |
| **Safety** | No loop limit | MAX_ITERATIONS guards the tool loop and the retry loop; failing tools return an error message instead of crashing |
| **Output** | Whatever LLM says | Validated, with an actual retry path back to `agent` |
| **Tool calls** | Run immediately, one at a time in practice | Run immediately (parallel if the LLM asks for several) *or* pause for human approval, opt-in per call |

## When to Use Each Pattern

- **Validation**: Always. Costs nothing, catches 80% of bad cases.
- **Context enrichment**: When you have a knowledge base (Qdrant, DB, API).
- **State tracking**: When you need loop control, retries, or multi-step logic.
- **Output gating**: When answer quality matters (not all use-cases need it).
- **Conditional routing**: When different queries need different paths (most real agents).
- **Error recovery**: Any tool that can fail — network calls, parsing, third-party APIs. Cheap to add via `handle_tool_errors`, so default to having it.
- **Human-in-the-loop**: Side-effecting or costly actions (sending, spending, deleting) — not needed for read-only tools like `search_docs`/`calculator`, which is why it's opt-in here rather than always-on.
- **Parallel tool execution**: Automatic — nothing to opt into, just don't assume you need to build it yourself.

## Extending Further

In production, you might still add:
- **Fallback node**: If primary path fails, try alternative.
- **Logging/monitoring node**: Track metrics, costs, latency.
- **Caching node**: Skip redundant searches for repeated queries.
- **A real HTTP resume flow**: `app/hitl_demo.py` resumes interrupts from a terminal `input()`. Doing this over `app/api.py` instead would mean returning the interrupt payload as an HTTP response and adding a second endpoint (e.g. `POST /chat/resume`) that accepts the decision and calls `Command(resume=...)` — not implemented here to keep the API surface small.

The key insight: **LangGraph lets you make every step of the pipeline explicit and controllable.** That's what separates it from "just LLM + tools."
