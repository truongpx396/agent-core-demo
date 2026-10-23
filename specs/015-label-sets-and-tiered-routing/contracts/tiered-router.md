# Contract: the tiered router in the graph

> **Superseded in part by spec 016 (`specs/016-decide-api/`).** The wire described here, `POST /classify` with `task: "label"`, no longer exists, nor does `/classify` at all (the unused trained-head path went too); the same matcher is now asked through `POST /v1/decide` (`specs/016-decide-api/contracts/decide.md`), and `ROUTER_LABELSET` became `ROUTER_LABELS`. The label-set file format, cutoffs, indexes and failover below are unchanged.

Replaces steps 1-6 of `specs/014-model-serving-eval-harness/contracts/graph-seam.md` (`route_model`) and the router rows of that spec's
`contracts/settings.md`. Everything else in the 014 seam stands and keeps its tests: `route_model` writes only `State.model_route`; it cannot touch
tool gating, approval, tenant scope or idempotency (014 FR-031..037); it is never reached for a refused input, a cache hit or a resumed turn; a
failure of the routed model repeats that one call on the local alias; nothing in routing raises.

## Tiers and aliases

| Tier | Rank | Alias | Configured by |
|---|---|---|---|
| `local` | 0 | `CHAT_MODEL` | always present (the default; the value `local` is what checkpoints and metrics already hold) |
| `medium` | 1 | `MEDIUM_CHAT_MODEL` | empty = this tier does not exist |
| `strong` | 2 | `STRONG_CHAT_MODEL` | empty = this tier does not exist |

`State.model_route` is `Literal["local", "medium", "strong"] | None`. A checkpoint without it, or holding an older value, runs local; a rollback to
code that only knows `local` and `strong` reads `medium` as "not strong", i.e. local. `STATE_SCHEMA_VERSION` is not bumped.

## Settings (`app/core/config.py`, `.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `ROUTER_MODE` | `off` | `off`: no work, no call, byte-identical to before. `shadow`: decide, count and log, run on the local alias. `on`: act. |
| `MEDIUM_CHAT_MODEL`, `STRONG_CHAT_MODEL` | empty | gateway aliases of the tiers above |
| `ROUTER_MAX_TIER` | `strong` | the ceiling: no turn is routed above it (one operator-wide value; per-tenant is a disclosed gap) |
| `ROUTER_LABELSET` | `route-topics` | the label set asked for the label signal; empty = rules only |
| `ROUTER_LONG_PROMPT_CHARS` | `1500` | the `long_prompt` rule's threshold (>= 200) |
| `ROUTER_TIMEOUT_SECONDS` | `0.5` | the label call's deadline; a miss leaves the rules to decide |

**Fail fast at import.** `ROUTER_MODE` other than the three values; `shadow` or `on` with neither a medium nor a strong alias; two tiers sharing an
alias (the local one included); a leftover `ROUTER_ENABLED=true` (the old switch; the message names `ROUTER_MODE`, because a switch that is silently ignored would turn a router
someone believed was on into one that is off). `ROUTER_ENABLED=false` and `ROUTER_THRESHOLD` changed nothing without it and are ignored. Messages
name variables, never their values.

## The decision (`app/core/routing_policy.py`: pure, imports nothing from the app)

```
decide(text, label, *, ceiling, configured, long_prompt_chars) -> Decision
  rule_tier  = max over the rules below                       (local when none fires)
  label_tier = LABEL_TIERS[label] if the label signal answered with a label, else local
  asked      = max(rule_tier, label_tier)
  tier       = the highest configured tier at or below min(asked, ceiling)
```

`Decision` records the tier, `asked`, each signal's tier, the rule ids that fired, the label, which signal decided (`rules`, `label`, `both` when
they agree above local, `none`) and whether the result was lowered by the `ceiling` or by an `unconfigured` tier.

**Rules** (a closed list of ids; each is a function of the user's current message only):

| id | Fires when | Tier |
|---|---|---|
| `long_prompt` | `len(text) >= ROUTER_LONG_PROMPT_CHARS` | medium |
| `code` | a fenced block, a traceback header, or three lines indented by four spaces or a tab | medium |
| `multi_part` | three or more `?`, or three or more enumerated or bulleted lines | medium |
| `reasoning_markers` | two distinct markers from a fixed list ("step by step", "prove", "derive", "trade-off", "root cause", "estimate", "optimize", "think through", ...) / three or more | medium / strong |

**Label table** (`LABEL_TIERS`; a test keeps its keys equal to the shipped set's labels): `smalltalk`, `lookup`, `action`, `drafting` -> local;
`analysis`, `code` -> medium; `reasoning`, `high_stakes` -> strong. These describe the kind of work a model must do, not how hard a message is: the
evidence that they predict where a stronger model answers better does not exist yet (`research.md`; spec 014 found no signal in the labels it had).
An unknown label, `null` and a failed call are all local.

## `route_model(state, config) -> {"model_route": tier}` (never raises)

```
1. ROUTER_MODE == off                       -> local, outcome=disabled          (no work, no call, no log)
2. text = the user's current message only   (no ctx, no history, no retrieved text); empty -> local, outcome=no_text
3. rules = evaluate(text)                   (microseconds, cannot fail)
4. label = POST /classify task=label within ROUTER_TIMEOUT_SECONDS, first 2000 characters, if ROUTER_LABELSET is set
              served_by none | timeout | error -> no label; outcome=label_none | label_timeout | label_error (the rules decide)
              a label the app has no tier for  -> no label; outcome=label_unknown
5. decision = decide(...)
6. for the chosen tier above local: refuse_unpriced(alias) -> step down one tier and look again; outcome=tier_unpriced
7. mode == shadow -> record the decision, return local   |   mode == on -> return the tier
```

One `agent_model_route_total{outcome}` increment per turn, the first that applies of `error`, `tier_unpriced`, `label_timeout|label_error|label_none|label_unknown`,
else `decided` (`disabled` and `no_text` end the node earlier). One `agent_model_route_tier_total{tier, mode, decided_by}` increment per turn that got past step 2: the tier chosen (in `shadow`, the
tier that WOULD have been used). The log line carries `outcome, mode, tier, asked, decided_by, limited_by, rules` (ids), `label`, `similarity_decile`,
`served_by`, `labelset_version`, `latency_ms`, `run_id`: never the text, the similarity itself, or anything about who asked.

## `agent()` and `metered_invoke`

```
alias = {"medium": MEDIUM_CHAT_MODEL, "strong": STRONG_CHAT_MODEL}.get(state.get("model_route")) or None
call  = metered_invoke(llm, messages, config=config, kind=kind, model_alias=alias or model_alias, invoke_model=alias)
```

With `alias is None` the call is byte-identical to a build without routing. If a routed call raises an error `strong_unreachable` recognises (a
connection failure, a timeout, any 5xx that is not the gateway's budget stop), that one call is repeated on the local alias, counted as
`agent_model_route_total{outcome="routed_unreachable_fell_back"}` and logged with the alias; the turn keeps its tier and the next call tries it
again. The ledger names the alias that served.

## Alerts

`ModelRouteStrongFailing` becomes `ModelRoutedTierFailing`: `sum(rate(agent_model_route_total{outcome="routed_unreachable_fell_back"}[10m])) > 0` for
10 m. (The old outcome name was `strong_failed_fell_back`; the rename is in the same change as the counter, and a test checks that every outcome an
alert names is one the code increments.)

## Invariants the tests pin

| # | Invariant | Tier |
|---|---|---|
| 1 | `off`: no network call, `model_route` is `local`, and `agent()` calls are byte-identical to a build without routing | unit |
| 2 | `shadow` makes the same decision as `on` (same metrics and log), returns `local`, and changes no gateway call or ledger row | unit |
| 3 | Each rule fires on its examples and not on near misses; the label table covers the shipped set exactly; `decide` is `min(ceiling, max(...))` for every combination of the two signals, the ceiling and the configured tiers | unit |
| 4 | A missing, slow, erroring or abstaining label leaves the rules to decide, with the right outcome | unit |
| 5 | An unconfigured or unpriced tier steps down one tier at a time, counted; an unpriced alias under `UNPRICED_MODEL_POLICY=block` never refuses the turn | unit |
| 6 | The routed alias is the one priced and recorded; a routed failure falls back once, counted, priced as local; a budget stop and a 4xx do not | unit |
| 7 | Tool gating, approval, tenant scope and idempotency are identical under all three tiers | graph |
| 8 | A resumed turn keeps its tier and makes no label call; a checkpoint with no tier, or with `medium` read by older code, runs local | graph / unit |
| 9 | The default suite makes no label call (the autouse guard), and each of `ROUTER_MODE`'s validation rules has a test | meta / unit |
| 10 | The text never reaches a log or a metric label | unit |
