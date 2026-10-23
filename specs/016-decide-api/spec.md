# Spec 016: the ML service speaks a typed-decision interface (`POST /v1/decide`)

**Status:** built, one PR. **Builds on:** spec 015 (label sets and the tiered router, #185). **Scope decision (the owner's words, 2026-10-23):** "add
`/v1/decide` as a replacement for `/classify`, the shape should be like working with the Jev API, but not yet integrate Jev."

## Why

Spec 015 gave the ML service a second task on `/classify`, with a wire shape of our own. A decision ("which topic is this?", "should a person take
over?") is a thing with a hosted market and a documented wire format. Speaking that format locally means the application asks its questions once, in a
shape another implementation also answers, and the day a hosted one is wanted the change is in one client function, not in every caller.

## What changes

1. **`POST /v1/decide`** on the ML service answers `choice` questions in the System One / Jev request and answer shape, from the label sets of spec 015
   (contract: `contracts/decide.md`).
1a. **`/classify` is deleted** (the owner's word, 2026-10-24: "delete leftover for now"). It served `task: "label"` (never released) and `task: "route"`, a
   trained routing head that spec 014 built, no person ever accepted and the app stopped asking for in spec 015. Gone with it: `classify.py`, the head
   files and `docker/ml-service/heads/`, `ROUTER_HEAD_DIR`, the metrics `ml_classifier_serving` and `ml_classify_requests_total`, the `classifier` key of
   `/health/models`, the alert `RouterClassifierOnFallback` (`MlServiceModelDown` still alerts when either model is out, after 5 minutes instead of 15),
   the harness's `service` backend and its candidate `ml-service-classify` (the app's own router is now the classifier baseline), and the head tests. The
   `classify` ROLE in `models.toml` and the `ML_CLASSIFY_*` settings stay: they name the models that serve decisions and their bounds, and renaming them is
   churn for operators with nothing gained.
2. **The app asks through a `Choice`** (`app/core/classification.py::choose`): a name, instructions and an option-to-description map. The model router's
   question is `routing_policy.ROUTE_INSTRUCTIONS` and `LABEL_DESCRIPTIONS` (same keys as `LABEL_TIERS`, pinned by a test).
3. **`ROUTER_LABELSET` becomes `ROUTER_LABELS`** (true/false: ask for the topic, or decide from rules alone). The name of a set was never a free choice: the
   tier table is written for the eight labels of `route-topics`.

## Requirements

- **FR-001** `POST /v1/decide` accepts `{model?, state, questions}` and answers `{model, answers, usage, x_served_by}` exactly as `contracts/decide.md` says; every
  rejection is a 4xx with a stable `error.code`, before any model is asked.
- **FR-002** Only `choice` is answered. `score`, `noul` and `assets` are 422, never silently ignored.
- **FR-003** A question's name is a label set's name and its `criteria` keys equal the set's labels; a mismatch is a 400 that names the difference.
- **FR-004** `choice` is `null` when the text is near none of the options. The answer carries no `confidence` and no `probabilities`.
- **FR-005** The questions of one request are answered by one model, with one embedding pass.
- **FR-006** No model able to answer is 503 `model_unavailable` with `Retry-After`; never a 500 and never a guess.
- **FR-007** The app's client sends the question and the first 2,000 characters of the message and nothing else (pinned by a test), validates what comes
  back (an answer to another question, a choice outside the options, a number out of range are errors), and reads only documented fields: an answer built
  from the hosted interface's documentation, with no `x_` field, is understood.
- **FR-008** No request text reaches a log; metrics carry the question's name only when the service knows it.

## Success criteria

- **SC-001** The behaviour of spec 015 is unchanged: the same matcher, cutoffs and failover answer, and the hermetic suite's service tests (the HTTP ones
  moved to the new endpoint, the matcher ones untouched) pass.
- **SC-002** Mutation check: every deliberate bug in `decide.py`, `Labeller.decide` and the client is caught (45 mutants, 45 killed, after closing three
  boundary gaps the first run found).
- **SC-003** Latency stays as spec 015 SC-003 states it (50 ms p95 on the reference machine, measured and recorded, not asserted on CI).

## Not built, and why

- **Jev itself.** The hosted interface was not called successfully: the supplied Opper key authenticates but the evaluation endpoint answers 402 until the
  account has a card. Every hosted field in the contract is from documentation. The swap checklist is in the contract.
- **`noul` and `score`.** A yes/no probability is a calibration problem (a fitted mapping from the two centroids' similarities, checked on held-out data);
  an ordered scale needs a different matcher. Both are 422 until something needs them.
- **Settings to point the app at another URL.** One client function changes at swap time, with the privacy and spend decisions beside it; shipping the
  knobs first would ship a way to send user text to a third party without those decisions.
