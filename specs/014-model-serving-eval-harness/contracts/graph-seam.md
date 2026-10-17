# Contract: the routing seam in the graph

The only place this feature touches `app/agent/`. Everything below is checked by a named test in `tasks.md`; the invariants are the
spec's FR-031..037.

## State

`State.model_route: Literal["local", "strong"] | None` — optional, checkpointed. **Not** a `STATE_SCHEMA_VERSION` bump
(`research.md` A6); a test resumes a checkpoint lacking the key.
`validate_input` writes `"model_route": None` each new turn (never on a resume, which skips it).

## Node `route_model`

Placement: after `check_semantic_cache` (miss) and `compact_history`, before `retrieve_context`; registered through `add(...)` so
`instrumented` wraps it. Behind `GraphProfile.include_router` (true for the parent graph, false for `SUBAGENT_PROFILE`).

```
route_model(state, config) -> {"model_route": "local" | "strong"}   # never raises
  1. not ROUTER_ENABLED or STRONG_CHAT_MODEL == ""            -> local, outcome=disabled        (no network call)
  2. text = the user's current message only                   (no ctx, no history, no retrieved text)
  3. await refuse_unpriced(STRONG_CHAT_MODEL)                 -> local, outcome=strong_unpriced
  4. POST ml-service /classify within ROUTER_TIMEOUT_SECONDS  -> any error / timeout -> local, outcome=classifier_error|classifier_timeout
  5. served_by == none or score is null                       -> local, outcome=no_advice
  6. score >= ROUTER_THRESHOLD                                -> strong, outcome=strong   else local, outcome=local
```

It is `async`, reads `ctx` for nothing, writes nothing but `model_route`, and logs only the metadata in `data-model.md` §9.
It is never reached for a refused input, a cache hit, or a resumed turn (graph edges); a test asserts all three by running the
graph and counting `/classify` calls.

## `agent()` and `metered_invoke`

```
route = state.get("model_route")
alias = STRONG_CHAT_MODEL if route == "strong" else model_alias        # model_alias is the graph's own, as today
call  = await metering.metered_invoke(llm, messages, config=config, kind=kind, model_alias=alias,
                                      invoke_model=(alias if route == "strong" else None))
```

- `metered_invoke(..., invoke_model=None)` adds `model=invoke_model` to the `ainvoke` keywords only when given; pricing and the
  usage event use `model_alias` as today. Default behavior with `invoke_model=None` is byte-identical to now.
- **Degrade to local.** If `route == "strong"` and the call raises an error in `STRONG_UNREACHABLE` (below), `agent()` increments
  `agent_model_route_total{outcome="strong_failed_fell_back"}`, logs once (metadata), and repeats the same call with
  `alias = model_alias` and `invoke_model=None`. It does not set `model_route` (the turn stays "strong" for accounting; the next
  `agent()` call in the same turn tries strong again and may fall back again, each counted — bounded by the existing iteration
  ceiling).
- `STRONG_UNREACHABLE` = `openai.APIConnectionError`, `openai.APITimeoutError`, and `openai.APIStatusError` with status 502, 503
  or 504. **Excluded:** `provider_budget_exceeded` (the gateway's budget stop, recognised by error `type`, never retried), every
  4xx, and any error after a response began.
- Compaction (`compact_history`) and follow-ups keep the local alias: they are not the answer.

## Invariants the tests pin

| # | Invariant | Test (tier) |
|---|---|---|
| 1 | With the router off or the alias empty, `agent()` calls are byte-identical to today (same kwargs, same alias, same ledger row) | unit |
| 2 | `route_model` never raises; each error path lands on `local` with the right outcome | unit |
| 3 | Not called on moderation block / cache hit / approval resume | graph |
| 4 | `SYSTEM_PROMPT` and the assembled system message are byte-identical under both routes | unit (extends `test_prompt_cache_stability`) |
| 5 | The same mutating tool call under both routes: same capability tier, same approval pause, same `ctx` scoping, same `idempotent()` key | graph |
| 6 | A resumed (post-approval) turn keeps `model_route`; `/classify` is not called again | graph |
| 7 | A checkpoint without `model_route` resumes and runs local | unit |
| 8 | `strong` + each `STRONG_UNREACHABLE` class falls back once, counted, priced as local; `provider_budget_exceeded` and a 400 do not | unit |
| 9 | With `UNPRICED_MODEL_POLICY=block` and an unpriced alias, the route is local and the turn is not refused | unit |
| 10 | The usage ledger records the alias that served | unit + integration |
| 11 | The default suite makes no `/classify` call (autouse mock) | meta-test |

## Metrics

`agent_model_route_total{outcome}`, `agent_model_route_latency_seconds`. Alert `ModelRouteStrongFailing`:
`sum(rate(agent_model_route_total{outcome="strong_failed_fell_back"}[10m])) > 0` for 10 m.
