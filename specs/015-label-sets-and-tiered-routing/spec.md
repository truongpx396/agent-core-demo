# Feature Specification: Label Sets and Tiered Model Routing

**Feature Branch**: `015-label-sets-and-tiered-routing`

**Created**: 2026-10-22

**Status**: Draft (this file is written before the code it describes; `tasks.md` records what was built and what was measured)

**Input**: "Properly complete the classification and model-routing work, the way a professional team would": classify a text into a predefined set of choices on the self-hosted ONNX embedding model (for an agent step such as "send an email or not", and for routing), and route a turn to a low, medium or strong model from rules plus those labels, with the policy in the app and the classifier in the ML service.

> **Why this follows spec 014 and not an amendment of it.** Spec 014 built the seam (`route_model` may only advise, one strong alias, every failure is "local") and a harness, then tried the only signal it had, a trained head, and found that the labels could not support one (`specs/014-.../spec.md` Status; `ENGINEERING_PATTERNS.md` pattern 69). Nothing in this spec revisits that finding. It builds the signals that need no training, the two-valued seam becomes three-valued, and the rollout is staged so that evidence arrives before cost does.

## What this feature is, in one paragraph

The ML service learns a second task on `POST /classify`: `task: "label"` takes the name of a **label set** (a reviewed JSON file of labels, each with a few example sentences) and answers which label a text is closest to and how close. There is no training: the service embeds the examples with the model that serves the request, averages each label's examples into one unit vector, and compares. The app uses it as one of two signals for routing. The other is a handful of **rules** over the prompt text (length, code, reasoning words, several questions). Each signal names the lowest tier it will accept; the turn goes to `min(ceiling, max(rules, label))` of three tiers (`local`, `medium`, `strong`), through the alias the operator configured for that tier. The router ships **off**; `shadow` computes, counts and logs every decision while the turn runs on the local model, so an operator reads what the router would have spent before it spends anything.

## Decisions, each with the evidence or the reason

| # | Decision | Why |
|---|---|---|
| D1 | A label's vector is the **centroid** of its examples (mean of unit vectors, renormalised), not the best single example or the mean of the best two. | Measured on CLINC150 with bge-small, anchors being the first *k* training examples per intent, accuracy on 1,800 test items: k=3 0.778 vs 0.749 (max) vs 0.775 (top-2); k=5 0.795 vs 0.761 vs 0.781; k=10 0.863 vs 0.806 vs 0.834. Centroid also rejected the most out-of-scope items at the same 95% in-scope keep rate (78% vs 71% at k=10). Caveat: 150 fine-grained intents is a harder task than seven topics, and one centroid per label assumes a label is one cluster of meaning. |
| D2 | The "no label" cutoff (`min_similarity`) is stored **in the label set, per model**, and a set is served only by a model that has an entry. | Similarity scales differ by model (the CLINC cutoffs were 0.69-0.75 for bge-small; MiniLM sits elsewhere), and the service's fallback is a different model. One app-side number would be wrong whenever the fallback answers. Same principle as a head bound to its model (014 FR-028). |
| D3 | Examples are stored as **text**, and the serving model embeds them. | A stored vector is only valid for one model revision, and the failure of a wrong one is confident nonsense with no error. Text cannot go stale; the cost is embedding ~80 short sentences once per model load. |
| D4 | The service returns facts (`label`, `similarity`, `runner_up`, `margin`); it never returns a tier. | The tier depends on the tenant's ceiling, which aliases exist and which are priced. The service receives the prompt and nothing else, by design (014 FR-031). The label-to-tier policy is app code. |
| D5 | `combine = min(ceiling, max(rules, label))`, and a signal that is missing contributes `local`. | `max` is the cost-safe reading of "either signal can ask for more"; `min` with an operator ceiling bounds spend. A missing signal must not cancel the other one. |
| D6 | Three tiers named `local`, `medium`, `strong`. `local` is the existing name for the default tier (the value is already stored in checkpoints and counted in metrics). | A rename would put two spellings of one thing in persisted state. In documents "low" and `local` are the same tier. |
| D7 | A tier with no configured alias, or an unpriced one, **steps down** to the next lower tier. | Never spend more than the operator configured. Counted, so a misconfiguration is visible. |
| D8 | `ROUTER_MODE=off\|shadow\|on`, default `off`. | The evidence in 014 is that routing quality could not be measured on the labels available. Shadow mode is how the quality question gets asked on real traffic without taking the risk. |
| D9 | The head scoring path (`task: "route"`) stays in the service and leaves the app. | The head trainer is withdrawn (014 T070/T076). The service keeps the path so an accepted head can still be served and the harness can still evaluate it; the app stops calling it because nothing can make it answer. Removing the path is a separate decision. |

## User Scenarios & Testing

### User Story 1 - An agent step picks one of a few predefined choices (Priority: P1)

A developer adds a step such as "does this message ask us to send an email, or only to look something up?". They write a label set (`email-intent.json`: two labels, ten sentences each), mount it, and call `classification.label("email-intent", text)`. They get a label, or `None` when the text is not close to either, within a fraction of a second, with no model call to a paid API and no training.

**Why this priority**: it is the capability the routing work stands on, it is useful alone, and it adds no spend.

**Independent Test**: start the service with a fake embedder and a two-label set; a text near one label gets that label, a text near neither gets `None`, an unknown set is a 400, a missing calibration for the serving model is `served_by: none`.

**Acceptance Scenarios**:

1. **Given** a loaded set and a text near one label, **When** `/classify` is called with `task: "label"`, **Then** the answer names that label with `served_by: primary`, the set's version and the similarity.
2. **Given** a text farther than the model's cutoff from every label, **Then** `label` is `null` and the similarity is still reported.
3. **Given** the primary model is slow or failed, **Then** the fallback answers with its own cutoff, or `served_by: none` if its entry is missing.
4. **Given** a set file that is malformed, **Then** that set is `refused` in `/health/models` and the other sets, the embeddings and the guard keep working.

### User Story 2 - A maintainer knows how good a label set is before an agent relies on it (Priority: P1)

A maintainer runs one command and gets the set's accuracy on held-out sentences, how it treats a sentence that belongs to no label, and a comparison with a majority-class and a shuffled control, on this repository's own wording (`route-topics`), with the same reviewed-flag and confidence-interval treatment as every other report.

**Independent Test**: the report for a perfect backend is `ok`, for a shuffled one is `worse`, and the control on the real dataset says so.

### User Story 3 - An operator turns on routing in stages (Priority: P1)

An operator sets the aliases for `medium` and `strong`, leaves `ROUTER_MODE=shadow`, and reads the counts by tier and by deciding signal. Every turn still runs on the local model. When the shadow figures are what they want, `ROUTER_MODE=on` makes the turn use the chosen alias. `off` returns to byte-identical behaviour at any time.

**Independent Test**: with `off`, no call is made and every gateway call is unchanged; with `shadow`, decisions are counted and the call is unchanged; with `on`, the call names the tier's alias and the ledger records it.

**Acceptance Scenarios**:

1. **Given** `shadow` and a long, code-containing prompt, **Then** `agent_model_route_tier_total{tier="medium",mode="shadow"}` increases and the gateway call names the default alias.
2. **Given** `on`, a `strong` decision and a tier ceiling of `medium`, **Then** the call goes to the medium alias.
3. **Given** `on` and the label call times out, **Then** the rules alone decide, the outcome says so, and the turn is not delayed beyond `ROUTER_TIMEOUT_SECONDS`.
4. **Given** `on` and the medium alias is unreachable, **Then** that call is repeated on the local alias and counted, exactly as the strong alias does today.
5. **Given** an unpriced alias under `UNPRICED_MODEL_POLICY=block`, **Then** the router steps down and the turn is not refused.

### User Story 4 - A maintainer evaluates the rules-and-labels router like any other (Priority: P2)

The same `decide` function the app runs is wrapped as a backend for the existing classifier runner, so its decisions are scored against labelled outcomes with the controls that exist. The expected result on today's outcomes is `INVALID`, with the explanation line; the value is that the next, better set of outcomes needs no new code.

## Requirements

### ML service

- **FR-001** `POST /classify` accepts `task: "label"` with `labelset` (a string) and `texts` (1..16, each at most `ML_CLASSIFY_MAX_CHARS`). `task: "route"` is unchanged. Any other task is `400 invalid_task`; a set the service does not know is `400 unknown_labelset`.
- **FR-002** The answer is always `200` for a valid request: `{served_by, model, labelset, labelset_version, results: [{index, label, nearest, similarity, runner_up, margin, truncated}]}`; `nearest` is always the closest label, `label` is `nearest` or `null` when the similarity is below the serving model's cutoff, `similarity` is the cosine to that label's centroid, `runner_up` and `margin` describe the second nearest.
- **FR-003** A label set is a JSON file `{schema_version: 1, name, description, labels: {<label>: [<example>, ...]}, calibration: {<model>: {min_similarity}}}`. Every rule is a refusal at load: the name matches the file name, at least two labels, each label 3..64 examples, an example is 1..300 characters, no example appears under two labels, at most 512 examples, every `min_similarity` is a number in (0, 1). A bad file is that set's `refused: <reason>` line, never a failed start. `labelset_version` is the first 12 hex characters of the sha256 of the canonical content.
- **FR-004** Each serving model embeds the set's examples itself and keeps one unit centroid per label. The index is built when the model is ready and again when it reloads or its revision changes; until then the model is skipped for that set. A model with no `calibration` entry is skipped for that set (`refused: no calibration for <model>`).
- **FR-005** Failover and the deadline are the classifier's: first usable `classify`-role model, then the next, `served_by: none` when none can answer, `ML_CLASSIFY_DEADLINE_MS` per model.
- **FR-006** No text is logged. Metrics carry `labelset` (a name the service already knows) and `served_by`. `/health/models` lists each set with its version, label count and per-model state.
- **FR-007** Sets are read from `LABELSET_DIR` (default: `docker/ml-service/labelsets/`, baked into the image).

### Evaluation

- **FR-010** A frozen dataset `route-topics` (dev/test, authored, `reviewed: false` until a person reads it) holds sentences with one label or `none`, disjoint from each other and from the label set's examples, with a manifest like every other set.
- **FR-011** `make model-eval KIND=classifier CANDIDATE=<set-candidate> DATASET=route-topics` scores a label set with `IntentSuite` (in-scope accuracy, macro-F1, calibration, out-of-scope handling), with a majority-class and a shuffled-predictions control, and marks the report `PROVISIONAL` while any item is unreviewed.
- **FR-012** The calibration written into a set is reproducible: a command prints the cutoff that keeps a stated share of in-scope `dev` items for a named model, and the report shows the cutoff used.

### Application

- **FR-020** `State.model_route` is `local | medium | strong`. A checkpoint with no value, or an older one, runs local. `STATE_SCHEMA_VERSION` is not bumped.
- **FR-021** Settings: `ROUTER_MODE`, `MEDIUM_CHAT_MODEL`, `STRONG_CHAT_MODEL`, `ROUTER_MAX_TIER`, `ROUTER_LABELSET`, `ROUTER_LONG_PROMPT_CHARS`, `ROUTER_TIMEOUT_SECONDS`. `ROUTER_ENABLED` and `ROUTER_THRESHOLD` are removed and a leftover `ROUTER_ENABLED` is a startup error that names its replacement (a silently ignored switch is worse than a refusal). `on` or `shadow` with no configured medium or strong alias, or an alias equal to another tier's, is a startup error.
- **FR-022** Rules are pure functions of the user's current message; each names a floor tier and a reason id from a closed list. The label signal maps a label to a tier through a table in code, which a test keeps equal to the shipped set's labels.
- **FR-023** `decide` is one pure function from `(rules, label, ceiling, configured tiers)` to a decision that records the tier, the tier each signal asked for, which signal decided, and whether the ceiling or a missing alias changed the result.
- **FR-024** The label call sends the user's current message (first 2,000 characters) and nothing else. Any failure of it leaves the rules to decide; nothing in routing raises.
- **FR-025** `shadow` runs the whole decision and records it; the turn uses the local alias. `off` does nothing and makes no call.
- **FR-026** In `on`, the turn's calls use the tier's alias, priced and recorded as that alias. A call that never reaches the routed model is repeated on the local alias (014 behaviour, now for either routed tier).
- **FR-027** Routing still cannot change tool gating, approval, tenant scope or idempotency (014 FR-031..037); the invariants table of `contracts/graph-seam.md` keeps its tests, extended to three tiers.
- **FR-028** Metrics: `agent_model_route_total{outcome}` (what happened), `agent_model_route_tier_total{tier,mode,decided_by}` (what it chose). The log line carries tier, rule ids, label, a similarity decile, mode and latency, never text.
- **FR-029** The classifier runner can score `decide` as a backend (`run(backend=...)`), using the same function and the same rule and label tables as the app.

## Success Criteria

- **SC-001** With the default settings every gateway call, state value and metric is what it was before this feature (a test compares the call keywords).
- **SC-002** A shadow run changes no answer and no ledger row compared with `off`.
- **SC-003** The label task adds at most 50 ms p95 on the reference machine once warm. Not claimed from a laptop run through Docker Desktop (014 recorded 49.7-51.8 ms quiet there).
- **SC-004** The report for the shipped set on `route-topics` test states in-scope accuracy with a confidence interval, the share of `none` sentences it rejects, and beats the majority-class control.
- **SC-005** Nothing in this feature claims that routing improves answers. The rules-and-labels backend on the real outcomes is reported as it comes out (014 found it `INVALID`).

## Not built, and disclosed

- **A measured benefit of routing.** The only outcomes that exist compare qwen2.5:3b with qwen2.5:7b, judged by the 7B, and the stronger model scored +0.032. They were produced before the aliases were changed to a 7B local and 14B strong pair. Shadow mode and a better labelling run are how this gets answered.
- **Per-tenant tier ceilings.** One operator-wide ceiling, like the one switch in 014.
- **Conversation context.** The label and the rules see the current message only; "yes, do it" has no topic and falls to the rules.
- **A multi-label or hierarchical set.** One label per text.
- **Removing the head-serving path** (D9).
- **A LiteLLM router alias as a chat alias.** Covered by a separate guard (the app prices it as a known $0), see `tasks.md`.
