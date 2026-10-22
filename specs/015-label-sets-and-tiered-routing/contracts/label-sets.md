# Contract: label sets on the ML service

Amends `specs/014-model-serving-eval-harness/contracts/ml-service.md`, whose `/classify` section said "a second task is a later spec's endpoint, not
a parameter the server guesses". This is that second task. It is a **named** `task` value on the same endpoint (one validation path, one failover
chain, one deadline), never inferred from the shape of a request; `task: "route"` is unchanged.

## `POST /classify`, `task: "label"`

Request: `{"task": "label", "labelset": "route-topics", "texts": ["…"]}`. `texts` is 1..16 non-empty strings, each at most `ML_CLASSIFY_MAX_CHARS`
(default 4000). `labelset` is required.

| Status | `error.code` | When |
|---|---|---|
| 400 | `invalid_task` | `task` missing or not `route` / `label` |
| 400 | `invalid_input` | not JSON; `labelset` missing, empty or not a string; `texts` empty, not strings, or an empty string |
| 400 | `batch_too_large` / `input_too_long` | over 16 texts / over `ML_CLASSIFY_MAX_CHARS` |
| 400 | `unknown_labelset` | no file of that name was found (a file that was found but refused is **not** this; see below) |

Response `200`, always, once the request is valid, including when nothing can answer:

```json
{"served_by": "primary" | "fallback" | "none",
 "model": "bge-small-en-v1.5", "labelset": "route-topics", "labelset_version": "6a3fe2f35057",
 "results": [{"index": 0, "label": "code", "nearest": "code", "similarity": 0.773,
              "runner_up": "lookup", "margin": 0.041, "truncated": false}]}
```

- `nearest` is always the closest label. `label` is `nearest` when `similarity` is at or above the serving model's `min_similarity`, else `null`.
  `similarity` is the cosine between the unit vector of the text and the unit centroid of that label. `runner_up` and `margin` (nearest minus
  second nearest) describe the second closest label. `truncated` is true when the model cut the text at its own `max_length`.
- `served_by: "none"`, `model: null`, `labelset_version: null` and every field of every result `null` (`truncated: false`) when the set is
  refused, no model has an index for it yet, or no model could answer within its deadline. This is a normal answer, not an error.
- The service returns facts. What a label means (a model tier, an email, a queue) is the caller's decision.
- Failover and deadline are the classifier's (`ML_CLASSIFY_DEADLINE_MS`, default 250, per model): the first usable `classify`-role model in
  `models.toml` order is the primary; a model that raises is `failed` and the next one answers; a model that only overruns the deadline is skipped
  for that request and stays `ready`. Each model answers with its **own** index and **own** cutoff.
- No text is logged. `ml_label_requests_total{labelset, served_by}` counts requests; `labelset` is a name the service already knows (a request
  for an unknown name is rejected before it is counted, so a caller cannot create series).

## The set file

`docker/ml-service/labelsets/<name>.json` (the image's `/app/labelsets`; `LABELSET_DIR` overrides). Format, limits and the reasons for them are
in `docker/ml-service/labelsets/README.md`, and every rule is a refusal at load (`ml_service/labelsets.py`, `parse_labelset`): schema version, a
name equal to the file name, 2..64 labels, 3..64 examples of 1..300 characters each, no example under two labels, 512 examples in all, and a
`calibration` entry (`min_similarity` strictly between 0 and 1) for every model that is to serve the set.

`labelset_version` is the first 12 hex characters of the sha256 of the canonical content (schema version, name, labels, examples, calibration),
excluding the description.

## `GET /health/models`

Gains `labelsets`: `{"<name>": {"version": "…", "labels": ["…"], "models": {"<model>": "loaded" | "pending: …" | "refused: …"}}}`, or
`{"<name>": {"state": "refused: <reason>"}}` for a file that was found but cannot be served, or `{"(directory)": {"state": "refused: unreadable (…)"}}`
when the directory cannot be listed. `pending: not indexed yet` / `pending: model not ready` / `pending: indexing failed (<class>)` clear on their
own: the index is built when a model is ready, and again when a model reloads under a different revision (the supervisor tick, once a second).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `LABELSET_DIR` | `/app/labelsets` | where `<name>.json` is read; sets are read at start |
| `ML_CLASSIFY_MAX_CHARS` / `ML_CLASSIFY_DEADLINE_MS` | 4000 / 250 | as for `route` |

## Invariants the tests pin (`tests/ml_service/test_labelsets.py`, `test_shipped_labelsets.py`; `tests/integration/test_ml_service_classify_container.py`)

| # | Invariant |
|---|---|
| 1 | The nearest label, the cutoff (inclusive), `runner_up` and `margin` are the cosine arithmetic, worked out by hand on axis-aligned vectors |
| 2 | A centroid is the renormalised mean of **unit** vectors, whatever the examples' lengths; the similarity is a cosine (a longer query is not nearer) |
| 3 | A model with no `calibration` entry does not serve the set and is not even asked to embed its examples; the fallback uses its own cutoff |
| 4 | A model reloaded under another revision is re-indexed; a stale index never answers; an unchanged one is not rebuilt |
| 5 | A primary that raises (`failed`), overruns its deadline (stays `ready`) or returns NaN is replaced by the fallback; none usable is a `200` with `served_by: "none"` |
| 6 | A malformed, non-text, unreadable or unlistable set is that set's `refused` line and nothing else |
| 7 | Every rule of the file format is a refusal; the version changes with the content and the cutoffs but not the description |
| 8 | No request text reaches a log; an unknown set name creates no metric series |
| 9 | `task: "route"` is unchanged; the shipped sets parse, cover every classify-role model, name no tenant, and share no sentence with the dataset that judges them |
| 10 | In the real image, the served similarity equals an independent computation from `/v1/embeddings` vectors and the set file (CI integration tier) |
