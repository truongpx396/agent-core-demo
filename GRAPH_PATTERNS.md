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
      should_continue?  ← Decide: tools? done? max iterations?
       ├─→ tools  ← Execute tool calls
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
| **State** | Just messages | Messages + context + iterations |
| **Nodes** | agent + tools | 7 nodes: validate_input, reject_input, retrieve_context, agent, tools, check_output, retry_output |
| **Flow** | LLM ↔ tools loop | Multi-stage pipeline with two real conditional gates |
| **Context** | LLM decides what to search | Pre-fetched, and actually injected into the LLM call |
| **Safety** | No loop limit | MAX_ITERATIONS guards both the tool loop and the retry loop |
| **Output** | Whatever LLM says | Validated, with an actual retry path back to `agent` |

## When to Use Each Pattern

- **Validation**: Always. Costs nothing, catches 80% of bad cases.
- **Context enrichment**: When you have a knowledge base (Qdrant, DB, API).
- **State tracking**: When you need loop control, retries, or multi-step logic.
- **Output gating**: When answer quality matters (not all use-cases need it).
- **Conditional routing**: When different queries need different paths (most real agents).

## Extending Further

In production, you might still add:
- **Error handling node**: Catch and recover from tool failures gracefully (e.g. if `search_docs` throws or Qdrant is unreachable).
- **Human-in-the-loop node**: Pause and ask for approval before acting (LangGraph's `interrupt`).
- **Fallback node**: If primary path fails, try alternative.
- **Logging/monitoring node**: Track metrics, costs, latency.
- **Caching node**: Skip redundant searches for repeated queries.

The key insight: **LangGraph lets you make every step of the pipeline explicit and controllable.** That's what separates it from "just LLM + tools."
