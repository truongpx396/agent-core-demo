# Implementation Plan: Label Sets and Tiered Model Routing

**Spec**: [spec.md](spec.md) · **Contracts**: [label-sets.md](contracts/label-sets.md), [tiered-router.md](contracts/tiered-router.md) · **Evidence**: [research.md](research.md)

## Summary

Three parts, in the order they depend on each other. (1) The ML service answers `task: "label"` from a reviewed label-set file: a centroid per label,
embedded by the serving model, a per-model cutoff. (2) The harness can score a label set on sentences with a known topic, and pick its cutoff on a
separate split. (3) The app routes a turn to one of three tiers from two signals (rules over the prompt, the label), behind `ROUTER_MODE`
`off | shadow | on`, with the policy in one pure module the harness runs too. Nothing here changes behaviour until an operator sets `ROUTER_MODE`.

## Constitution check

| Principle | How this feature touches it |
|---|---|
| I Tenant isolation | The label call sends the user's current message and nothing else: no tenant, principal, history or retrieved text (014 FR-031, kept). Label sets are global configuration, not tenant data. |
| II Mandatory approval | Routing chooses a model for the reply; it cannot see or change which tools a turn may call or whether a write pauses for approval. A graph test runs the same mutating call under all three tiers. |
| III Typed tools | No tool is added. |
| IV Exactly-once | No write is added; `idempotent()` is unaffected. A routed-call retry on the local alias is a model call, not a side effect, and is not metered twice (a failed call records nothing). |
| V Bounded, observable failure | Every wait has a ceiling: the label call (`ROUTER_TIMEOUT_SECONDS`), the service's per-model deadline (250 ms), index building (30 s). Every degradation is a counted outcome; nothing in `route_model` raises; a bad label set is one `refused` line, not a failed start. |
| VI Untrusted content | A prompt is user data: never logged, never a metric label, never stored by the service. A label set is data, strict JSON, never code. |
| VII Tests with real backends | Hermetic tests for the logic; a container test for the real ONNX path against an independent computation; the harness for quality. The default suite makes no label call. |
| VIII Honest gaps | `spec.md` "Not built" and `research.md` limits; the PR bodies repeat them. |

## Source layout

```text
docker/ml-service/ml_service/labelsets.py      # NEW  parse + index + match + failover for task=label
docker/ml-service/ml_service/classify.py       # task dispatch; "route" unchanged
docker/ml-service/labelsets/route-topics.json  # NEW  the shipped set (8 topics, 80 examples, 2 cutoffs); README.md beside it
scripts/model_eval/runners/topics.py           # NEW  score a label set (IntentSuite) and calibrate its cutoff
scripts/model_eval/build/topics.py             # NEW  build evals/datasets/route-topics from evals/sources/route-topics/prompts.tsv
app/core/routing_policy.py                     # NEW  (PR 2) rules, label table, decide(): pure
app/core/classification.py                     # NEW  (PR 2) the app's client for task=label; never raises
app/agent/graph/model_route.py                 # (PR 2) three tiers, modes, price step-down
app/agent/graph/agent_node.py                  # (PR 2) the routed alias and its fallback
```

## Delivery

More than one reviewable change (CLAUDE.md: about 400 hand-written lines, 1,000 at most), split at the dependency:

1. **PR 1: label sets** (T101-T113): the spec, the service task, the shipped set, the dataset, the harness runner and calibration. Changes nothing the app does.
2. **PR 2: the tiered router** (T114-T124): policy, client, graph, settings, metrics, alert, the harness candidate, the evidence run. Off by default.
3. **PR 3: a guard for gateway-side router aliases** (T125): small, independent.

The branch is stacked on #179 (the harness it extends); PR 1 is restacked onto `main` when #179 merges.

## Risks, and what answers them

| Risk | Answer |
|---|---|
| The labels do not predict where a stronger model wins (014's finding) | `shadow` first; the tier table is a policy and is called one; no claim of improved answers anywhere (SC-005) |
| Cutoffs chosen on a small dev split generalise badly (22% of test in-scope sentences abstained against 6% on dev) | Disclosed in `research.md`; `make labelset-calibrate` makes re-choosing cheap; a larger dev set is the fix |
| The wrong label sends a cheap message to a paid tier | One of two signals; the ceiling bounds spend; `agent_model_route_tier_total` shows the share per tier in `shadow` before anything is paid |
| A model reload changes what the examples mean | The index is rebuilt from text whenever the revision changes (T102, test 4) |
