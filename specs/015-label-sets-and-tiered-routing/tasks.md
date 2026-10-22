# Tasks: Label Sets and Tiered Model Routing

`[X]` = built and tested. `[ ]` = not yet. **HUMAN** = only a person can do it; an agent must not tick it.

## Phase 1: the ML service (PR 1)

- [X] T101 `ml_service/labelsets.py`: the file format and its refusals, `parse_labelset`, `read_labelsets`, the content version
- [X] T102 The per-model index (centroids from the model's own embeddings), rebuilt on a revision change, built at start and on the supervisor tick
- [X] T103 `label_results` and `Labeller.label`: nearest, cutoff, runner-up, margin; failover, deadline, `served_by: none`; no text logged
- [X] T104 `/classify` dispatches on a named `task`; `labelset` validation; `unknown_labelset`; `ml_label_requests_total`; `/health/models` `labelsets`; `LABELSET_DIR`
- [X] T105 `docker/ml-service/labelsets/route-topics.json` (8 topics, 90 examples: 80 first, ten `lookup` examples added after the first look at the repository's own prompts, `research.md` R4) and its README
- [X] T106 Hermetic tests: 60 in `tests/ml_service/test_labelsets.py`, the shipped sets in `test_shipped_labelsets.py`; a container test against an independent computation (CI tier)
- [X] T107 Mutation check of the new service code: 31 mutants, all killed after three test gaps were closed (inclusive cutoff, centroid arithmetic, the `failed` state) and one redundant clause was removed

## Phase 2: measuring a label set (PR 1)

- [X] T108 `evals/datasets/route-topics/v1` (152 authored sentences, dev 56 / test 96, `none` included), its builder and source, kind `topics`
- [X] T109 `runners/topics.py`: scores a set in-process with the service's own code through `IntentSuite`, with the shuffled-predictions control; refuses a model with no cutoff
- [X] T110 `make labelset-calibrate`: the cutoff that keeps a stated share of in-scope dev sentences, rounded down; never reads the reported split
- [X] T111 Two classifier candidates (`labelset-bge-small-en-v1.5`, `labelset-all-minilm-l6-v2`) pinned to `models.toml` by a test
- [X] T112 `research.md`: the centroid-versus-nearest measurement with its script, the route-topics results, the margin finding
- [ ] T113 **HUMAN** Read `evals/sources/route-topics/prompts.tsv` and the 90 examples; fix what a user would not say; set `reviewed` (every report is PROVISIONAL until then). The same question as 014's T045 applies: flipping `reviewed` changes the manifest hash, and the README says add `v2/` rather than edit in place.

## Phase 3: the tiered router (PR 2)

- [X] T114 `app/core/routing_policy.py`: rules, `LABEL_TIERS`, `decide`; a sweep over every combination of rule tier, label, ceiling and configured tiers against an independent oracle (407 tests); mutation-checked with the rest of the app code (54 mutants, all killed after two test gaps were closed and two redundant lines removed)
- [X] T115 `app/core/classification.py`: the client for `task: "label"` (never raises; first 2,000 characters; deadline; the answer validated); the head-score client `routing_advice.py` and its tests are removed
- [X] T116 Settings and their validation (`ROUTER_MODE`, `MEDIUM_CHAT_MODEL`, `ROUTER_MAX_TIER`, `ROUTER_LABELSET`, `ROUTER_LONG_PROMPT_CHARS`); `ROUTER_ENABLED=true` refused, `ROUTER_THRESHOLD` and `ROUTER_ENABLED=false` ignored; `.env.example`
- [X] T117 `route_model` with three tiers, the three modes, the per-tier price step-down, the metrics (`agent_model_route_total` outcomes, `agent_model_route_tier_total`) and the log line
- [X] T118 `agent()` calls the routed alias and falls back for either routed tier; `ModelRouteStrongFailing` becomes `ModelRoutedTierFailing`, and `ModelRouterLabelUnavailable` is new
- [X] T119 The graph invariants of the contract: the same mutating call under all three tiers; shadow versus off; resume; checkpoint compatibility; the default suite makes no label call
- [X] T120 The harness runs `decide` as a router (`TieredRouter`, candidate `rules-and-labels-bge-small-en-v1.5`) and the evidence run on the 014 outcomes is recorded **as it came out: INVALID** (the strong model is +0.032 above the local one, so even a perfect router cannot be told from a random one), average gap recovered 0.42 against 0.50 for random with an interval of -0.43 to 1.22. On the repository's own 123 prompts the label signal first routed 11% up, mostly wrongly, and 5% after one round of added examples (`research.md` R4)
- [X] T121 GRAPH_PATTERNS.md pattern 68 rewritten; ENGINEERING_PATTERNS.md pattern 75; CLAUDE.md, README, the 014 contracts marked as superseded where they are; the constitution amended to 1.3.0 (two routed aliases)
- [ ] T122 **HUMAN** Decide `ROUTER_MODE=shadow` for a real deployment, read `agent_model_route_tier_total` for a week, then decide on `on` (and `ROUTER_MAX_TIER`)
- [ ] T123 **HUMAN** The constitution amendment (1.3.0) was written by hand: CLAUDE.md says to amend it through `/speckit-constitution`, which was not available to the author. Re-run it, or accept the wording in review.

## Phase 4: guard (PR 3)

- [ ] T125 A gateway-side router alias (priced as a known $0 by `/model/info`) is refused as a chat alias, or its cost is read from the response header; decided when PR 3 is written

## Decisions that are the maintainer's

- **HUMAN** The tier table (`LABEL_TIERS`) and the rules' thresholds are policy. The defaults are the author's judgment, with the evidence in `research.md`; change them knowingly.
- **HUMAN** `MEDIUM_CHAT_MODEL` / `STRONG_CHAT_MODEL` for production (the Gemini keys), and when to move `shadow` to `on`.
