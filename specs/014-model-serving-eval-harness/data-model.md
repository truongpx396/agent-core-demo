# Data Model: Model Serving Seams and an Evaluation Harness

> **Superseded in part.** The trained routing head and `POST /classify` described here were deleted in spec 016 (`specs/016-decide-api/`); the app's router is rules plus a topic label (spec 015), asked through `POST /v1/decide`. This document is kept as the record of what was built and why.

Shapes only — fields, rules, states. How they are produced is in `research.md`; the wire formats are in `contracts/`.
JSON examples are abbreviated. Every record that is stored carries a `schema_version` so the harness can refuse one it does not
understand rather than misread it.

---

## 1. Model candidate declaration (FR-007)

File: `evals/candidates/<kind>.toml`, one `[[candidate]]` table per model. Declaring a model never touches metric, dataset or
report code.

| Field | Type | Rule |
|---|---|---|
| `name` | str | unique within the kind; `[a-z0-9._-]+`; this is the key in baselines and reports |
| `kind` | enum | `embedding` \| `reranker` \| `screen` \| `classifier` |
| `path` | enum | `gateway` (the app's own path) \| `service` (an ml-service endpoint) \| `inprocess` (load it here) |
| `source.repo` / `source.file` / `source.revision` | str | required for `inprocess`; `revision` is a full commit sha, never a branch |
| `source.alias` | str | required for `gateway` (the LiteLLM alias) |
| `source.model` | str | required for `service` (the name the endpoint serves) |
| `prepare.query_prefix` / `prepare.document_prefix` | str | embedding only; default `""` |
| `prepare.pooling` | `cls`\|`mean`\|`last` | embedding/classifier, `inprocess` only |
| `prepare.normalize` | bool | default true |
| `prepare.max_length` | int | ≤ the model's own limit |
| `licence` | str | SPDX id or `other:<text>`; **required** — a candidate with none is refused |
| `notes` | str | free text |

**Validation.** `inprocess` requires repo, file and revision; `licence` is checked against the same allow-list as datasets only for
a *warning* (a model's licence is reported, never a reason to refuse to measure it).

## 2. Dataset manifest (FR-009, FR-010)

Directory: `evals/datasets/<name>/<version>/` with `manifest.json` and the data files. A public dataset has no data files in the
repo — only its lock entry (§3).

```json
{
  "schema_version": 1,
  "name": "retrieval-domains",
  "version": "v1",
  "kind": "embedding",
  "domains": ["ecorp", "support", "ops", "sales"],
  "licence": "MIT",
  "provenance": {
    "generator": "ollama/qwen2.5:7b", "prompt_sha256": "…", "generated_at": "2026-10-20",
    "filters": {"max_verbatim_span_chars": 40, "dedupe": true},
    "reviewed_share": 0.0
  },
  "splits": {"dev": {"file": "dev.jsonl", "sha256": "…", "items": 60},
             "test": {"file": "test.jsonl", "sha256": "…", "items": 120}},
  "corpus": {"file": "corpus.jsonl", "sha256": "…", "chunks": 412}
}
```

Item schemas (JSON Lines):

| Kind | File | One line |
|---|---|---|
| embedding | `corpus.jsonl` | `{"id","domain","text","source"}` — chunks made with the app's own chunker |
| embedding | `<split>.jsonl` | `{"qid","domain","query","relevant":{"<chunk id>": gain(int≥1)},"kind":"direct"\|"adversarial","reviewed":bool}` |
| reranker | `<split>.jsonl` | `{"qid","query","candidates":[{"id","text","gain"(0 if not relevant)}]}` — fixed list size `K` (manifest `list_size`), built from a model-independent lexical ranking |
| screen | `<split>.jsonl` | `{"id","text","label":"attack"\|"benign","domain","technique"?,"reviewed":bool}` |
| classifier | `<split>.jsonl` | `{"id","text","label":str,"domain"?}` (method sanity sets) |
| route | `prompts.jsonl` | `{"id","domain","text"}`; **outcomes** are separate, never part of the manifest hash: `outcomes/<strong>.jsonl` `{"id","local":0..1,"strong":0..1,"judge":str}` |

**Rules.**
- `dev` and `test` are disjoint by `id` **and** by near-duplicate text (normalised-token Jaccard ≥ 0.9 across the two is reported
  and fails verification).
- Every `sha256` is recomputed on load; any mismatch stops the run (no partial report).
- `reviewed_share` is computed from the items, not typed; a baseline recorded while it is below 1.0 is `provisional`.
- A repo-own set contains no real tenant text (FR-039): its provenance must name a generator or an author, and a test scans for the
  tenant/principal identifiers the seed data uses.

## 3. Public dataset lock entry (FR-009)

File: `evals/datasets/public.lock.json` — a list of:

```json
{"id": "beir-scifact", "kind": "embedding", "hub_repo": "BeIR/scifact", "revision": "<commit sha>",
 "files": [{"path": "corpus/corpus-00000-of-00001.parquet", "sha256": "…"}],
 "licence": "CC-BY-SA-4.0", "licence_source": "Hub card, read 2026-10-17", "redistribute": false,
 "cache_dir": "var/model_eval/public/beir-scifact"}
```

**Rules.** `licence` is mandatory and must be in the allow-list (`CC-BY-4.0`, `CC-BY-3.0`, `CC-BY-SA-4.0`, `Apache-2.0`, `MIT`,
`ODC-BY`); `redistribute` is always false (the cache is untracked); a fetched file whose hash differs from the lock stops the run.

## 4. Baseline record (FR-006)

File: `evals/baselines/<kind>/<candidate>@<dataset>-<version>.json`.

```json
{
  "schema_version": 1, "kind": "embedding", "candidate": "nomic-embed-text@ollama",
  "dataset": {"name": "retrieval-domains", "version": "v1", "reviewed_share": 0.0},
  "provisional": true,
  "model": {"identity": "ollama/nomic-embed-text", "revision": null, "licence": "Apache-2.0", "size_bytes": null, "dim": 768},
  "path": "gateway", "repo_revision": "c4fcc64",
  "quality": {"recall@5": {"mean": 0.0, "ci": [0.0, 0.0]}, "ndcg@10": {…}, "mrr@10": {…}},
  "per_query": "evals/baselines/embedding/…/per_query.jsonl",
  "efficiency": {"hw-3fa1c2d4": {"query_ms": {"p50": 0, "p95": 0}, "ms_per_chunk": 0, "rss_mb": null, "run_seconds": 0}},
  "recorded_at": "2026-10-20"
}
```

**Rules.** Quality is hardware-independent; `efficiency` is an object **keyed by hardware id** (a second machine adds a key).
`per_query` holds the per-query metric values the paired bootstrap needs, so a comparison never re-runs the baseline. A baseline is
immutable: re-recording writes a new file and the old is kept in git history. A comparison against a baseline whose `dataset`
(name + version) differs from the candidate's run is refused, not warned.

## 5. Evaluation report (FR-001, FR-002, FR-005, FR-014)

Written as `var/model_eval/reports/<kind>-<candidate>-<timestamp>.json` plus a rendered `.md`; schema in
`contracts/model-eval.md`. Fields: everything in §4 for the candidate, plus `baseline` (name, file, hw id used), `deltas`
(`{metric: {delta, ci, verdict: better|worse|inconclusive, needed_n}}`), `controls` (name → expected verdict → observed),
`tradeoffs` (a fixed list of one-line statements: quality, speed, size, licence, and for an embedding model the dimension-implied
storage per million chunks and the re-embed requirement), `warnings` (`PROVISIONAL`, latency compared across hardware,
contaminated, unreviewed share).

## 6. Embedding identity (FR-018)

```
EmbeddingIdentity(alias, resolved_model | None, dim, query_prefix, document_prefix, revision_label)
hash8 = sha256(canonical_json(resolved_model or "alias:<name>", dim, query_prefix, document_prefix, revision_label))[:8]
```

| Where recorded | How |
|---|---|
| Qdrant collection | the physical collection's name suffix: `<name>-<hash8>`; the live alias `<name>-live` points at it |
| Redis answer cache | the index name `idx:semantic_cache:<hash8>` and key prefix `cache:<hash8>:` |
| Human-readable copy | the collection `metadata` field when the server supports it (diagnostics only; never authoritative) |

**States of a collection relative to the configured identity.**

```
absent ──ensure_collection──▶ verified ◀──────────re-embed repoint─────────┐
legacy (no alias) ──ensure_live (adopt, warn)──▶ adopted ──re-embed──────▶ verified
verified | adopted ──config change──▶ mismatch ──re-embed──▶ verified
```

- `verified`: suffix equals the configured hash. `adopted`: legacy collection assumed to match (counted, warned). `mismatch`: reads
  raise `EmbeddingIdentityMismatch` (absorbed by the existing degrade callers), writes are refused.
- If `resolved_model` cannot be obtained, **the alias name stands in for the model** and the check proceeds, counted `unverified`.
  *(Changed during implementation: this said "writes are refused". That would have broken every deployment whose `OPENAI_API_BASE`
  points straight at an OpenAI-compatible server with no LiteLLM admin endpoint, which is how the live test tier runs. Weaker, but
  stable per deployment; `EMBED_MODEL_REVISION` covers the same name re-pulled to different weights.)*

## 7. Classifier head (FR-028, FR-030, FR-038)

Files under `ml_service/heads/` (shipped in the image, or mounted from `ROUTER_HEAD_DIR`):
`head-<model-name>.npz` (`w: float32[dim]`, `b: float32`) and `head-<model-name>.json`:

```json
{
  "schema_version": 1, "head_version": "2026-10-20.1", "model": "bge-small-en-v1.5", "model_repo": "BAAI/bge-small-en-v1.5",
  "model_revision": "5c38ec7c40…(full sha)", "model_file": "onnx/model.onnx", "dim": 384,
  "labels": {"0": "local_ok", "1": "needs_stronger"},
  "trained_on": {"dataset": "route-domains", "version": "v1", "outcomes_sha256": "…", "judge": "…", "margin": 0.2, "items": 0},
  "heldout": {"accuracy": 0, "macro_f1": 0, "ece": 0, "pgr_curve": [[0.0, 0.0]], "cpt": {"50": 0, "90": 0}},
  "controls_beaten": {"random": true, "length": true},
  "threshold": {"value": 0.5, "escalation_share": 0.0, "justification": "evals/reports/…"},
  "trained_at": "…", "seed": 20261017, "accepted": false
}
```

**Rules.** The service loads a head only if `model` (the registry name), `model_repo`, `model_file`, `model_revision` (the commit the Hub cache REALLY holds, read from the downloaded file's `snapshots/<sha>/` path and not taken from the registry) and `dim` equal the loaded model's **and `accepted` is the JSON value `true`** (the string "true" or the number 1 is refused), and the files are well formed (`schema_version` 1, `head_version` a non-empty string returned to the caller as `head_version`, `threshold.value` in [0, 1], `w` a finite `float[dim]` and `b` a finite scalar, read with `allow_pickle=False`; a damaged file is `refused: malformed (…)`, never a crash),
else it is `refused` (`revision mismatch` / `not accepted`) and that model has "no advice". `accepted` is set by a person after the harness
shows `controls_beaten` — the trainer will not write `accepted: true` itself (FR-038), and a test-only fixture may. The app never reads this file; it receives
only a score and the `served_by` / `head_version` from `/classify`.

## 8. ml-service model registry (FR-021, FR-022, FR-024)

File: `docker/ml-service/models.toml`. One table per served model:

| Field | Rule |
|---|---|
| `name` | the string clients send as `model`; unique |
| `roles` | subset of `embed`, `classify` (a model may serve both and is then loaded once) |
| `repo`, `file`, `revision` | `revision` full sha; `file` the exact ONNX file |
| `pooling`, `normalize`, `max_length`, `pad_token` | read from the model's own config at declaration time and checked at load |
| `needs_prefix` | documentation only — echoed by `/v1/models`, **never applied by the server** |
| `licence` | required |
| `enabled` | env override `ML_MODELS_ENABLED` (comma list) wins |

**Model load state machine** (per model; reported by `/health/models` and `/metrics`):
`not_loaded → loading → ready`; `loading → failed` (reason recorded) `→ loading` after a backoff of 5 s, 10 s, 20 s … capped at
300 s; `ready → failed` on a run error; one model's state never affects another's endpoint.

## 9. Routing decision (FR-027, FR-031, FR-033, FR-034)

`State.model_route: Literal["local", "strong"] | None` (new, optional, checkpointed). `None` means "no decision this turn" and is
read as local. Reset to `None` by `validate_input`. Set by the `route_model` node only. Per decision the node also emits (metrics
and a log line, **metadata only**): `outcome`, `served_by` (`primary`/`fallback`/`none`), `head_version`, `score_band`
(deciles), `latency_ms`.

Outcomes (the `agent_model_route_total{outcome}` label): `local`, `strong`, `no_advice`, `disabled` (router off or no alias),
`strong_unpriced`, `classifier_error`, `classifier_timeout`, `strong_failed_fell_back`.

## 10. Metrics added (Constitution V: a metric on every degrade path)

| Metric | Labels | Emitted by | Alert |
|---|---|---|---|
| `agent_model_route_total` | `outcome` | app (`route_model`, `agent`) | `ModelRouteStrongFailing` (the two degrade outcomes) |
| `agent_model_route_latency_seconds` | — | app | — |
| `agent_embedding_identity_total` | `outcome` (`verified`,`adopted`,`mismatch`,`unverified`), `collection` | app | `EmbeddingIdentityMismatch` |
| `ml_model_state` (gauge, 1 = ready) | `model` | ml-service | `MlServiceModelDown` |
| `ml_classifier_serving` (gauge) | `served_by` | ml-service | `RouterClassifierOnFallback` |
| `ml_embed_requests_total`, `ml_embed_items_total`, `ml_embed_errors_total` | `model` | ml-service | via `MlServiceModelDown` |
| `ml_embed_latency_seconds`, `ml_embed_batch_size` | `model` | ml-service | — |
| `ml_classify_requests_total` | `served_by` | ml-service | — |
