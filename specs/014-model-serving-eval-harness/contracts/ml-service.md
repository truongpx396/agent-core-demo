# Contract: ml-service endpoints

> **Superseded in part.** The trained routing head and `POST /classify` described here were deleted in spec 016 (`specs/016-decide-api/`); the app's router is rules plus a topic label (spec 015), asked through `POST /v1/decide`. This document is kept as the record of what was built and why.

Base URL: `ML_SERVICE_URL` (dev `http://localhost:8083`, in-network `http://ml-service:80`). No authentication (internal network,
as today). `/rerank` and `/prompt-guard` are **unchanged**; this file defines what is new or changed.

Errors use `{"error": {"code": str, "message": str}}` with the HTTP status; `code` values below are stable and tested.

---

## `GET /health` — liveness (unchanged)

`200 {"status": "ok"}` whenever the process answers. Docker's healthcheck keeps using it. It does **not** mean every model is
ready (a model that failed to load must not restart the container; FR-024).

## `GET /health/models` — per-model state (new)

```json
{"status": "ok" | "degraded",
 "models": {
   "bge-small-en-v1.5": {"state": "ready", "roles": ["embed", "classify"], "revision": "5c38ec7c…", "dim": 384, "since": "…"},
   "all-MiniLM-L6-v2":  {"state": "failed", "reason": "OSError: …", "retry_in_s": 40, "roles": ["classify"]},
   "rerank":            {"state": "ready"}, "prompt-guard": {"state": "ready"}
 },
 "classifier": {"serving": "primary" | "fallback" | "none", "heads": {"bge-small-en-v1.5": "loaded", "all-MiniLM-L6-v2": "refused: revision mismatch" | "refused: not accepted"}}}
```

`200` always (it is a report). `status` is `degraded` if any declared, enabled model is not `ready`. The app's readiness check
reads it (`checks.ml_service` stays the liveness answer; the detail goes in `warnings`, never turning the API unready).

## `GET /metrics` — Prometheus text (new)

Exposes the `ml_*` metrics in `data-model.md` §10. Scraped by a new `ml-service` job.

---

## `POST /v1/embeddings` — OpenAI-compatible (new, FR-021)

Request:

```json
{"model": "bge-small-en-v1.5", "input": "text" | ["text", "…"], "encoding_format": "float" | "base64"}
```

| Rule | Behavior |
|---|---|
| `model` unknown or not enabled | `404 model_not_found` (message names the served models) |
| `model` not `ready` | `503 model_unavailable` with `Retry-After` = `retry_in_s` |
| `input` empty list, empty string, or not str/list[str] | `400 invalid_input` |
| items > `ML_EMBED_MAX_ITEMS` (default 256) | `400 batch_too_large` (never truncated) |
| any item > `ML_EMBED_MAX_CHARS` (default 8000) | `400 input_too_long` naming the index |
| `dimensions` present | `400 dimensions_unsupported` |
| `encoding_format` absent | `float` |
| `encoding_format` other than the two | `400 invalid_input` |

Response `200`:

```json
{"object": "list", "model": "bge-small-en-v1.5",
 "data": [{"object": "embedding", "index": 0, "embedding": [0.0123, …]}],
 "usage": {"prompt_tokens": 17, "total_tokens": 17}}
```

- `data` is in the **same order** as `input`, `index` is the position.
- Vectors are L2-normalised iff the model's registry entry says so; the dimension is the model's.
- `base64`: the float32 little-endian bytes of the vector, standard base64, in `embedding` (as the hosted APIs do).
- `prompt_tokens` counts tokens actually fed to the model after truncation to the model's `max_length`; **truncation to
  `max_length` is applied and reported** in a response header `X-Truncated-Items: <n>` (the *request* ceilings above are
  rejections; the model's own context limit is a documented truncation, as the existing guard does).
- Concurrency: concurrent calls are coalesced per model (one pass at a time per model, `MAX_BATCH_WAIT_MS` window).

**Compatibility acceptance test (hermetic).** The official `openai` SDK (`AsyncOpenAI.embeddings.create`, default arguments, i.e.
base64 requested and decoded) pointed at the ASGI app in-process returns the same vectors as `encoding_format="float"`, and
`langchain_openai.OpenAIEmbeddings.aembed_documents` works against it.

## `GET /v1/models` — served models (new)

`200 {"object": "list", "data": [{"id": "bge-small-en-v1.5", "object": "model", "owned_by": "ml-service", "dim": 384,
"max_length": 512, "needs_prefix": {"query": "Represent this sentence…", "document": ""}, "licence": "MIT",
"revision": "5c38ec7c…"}]}`. `needs_prefix` is **documentation for the operator**; the server never applies it.

---

## `POST /classify` — routing advice (new, FR-027..029)

Request: `{"texts": ["…"], "task": "route"}` (`task` is required and must be `route`; a second task is a later spec's endpoint,
not a parameter the server guesses. **Amended by spec 015:** the second task is `label`, named in the request and never inferred; see
`specs/015-label-sets-and-tiered-routing/contracts/label-sets.md`). `texts` 1..16 items, each ≤ `ML_CLASSIFY_MAX_CHARS` (default 4000), else `400`.

Response `200` (always, including when no model can answer):

```json
{"served_by": "primary" | "fallback" | "none",
 "model": "bge-small-en-v1.5", "head_version": "2026-10-20.1",
 "results": [{"index": 0, "decision": "local_ok" | "needs_stronger" | "none", "score": 0.83, "truncated": false}]}
```

- `score` is the head's probability that the prompt `needs_stronger` (0..1); **the threshold is the caller's** (`ROUTER_THRESHOLD`).
  `decision` is `needs_stronger` iff `score ≥` the head's recorded `threshold.value`, provided for convenience and tests; the app
  decides from `score`.
- `served_by: "none"`, `decision: "none"`, `score: null` when neither model has a `loaded` head. **A head whose JSON says `"accepted": false` is never `loaded`** (`refused: not accepted`): a trained but unvalidated head cannot influence a turn.
- Failover: see `data-model.md` §8 and `research.md` C2. A primary that is `ready` but whose call exceeds `ML_CLASSIFY_DEADLINE_MS`
  (default 250) is abandoned for that request and the fallback answers; the primary stays `ready`.
- No text is logged. Metrics carry `served_by` only.

## Environment (ml-service container)

| Variable | Default | Meaning |
|---|---|---|
| `ML_MODELS_ENABLED` | the registry's `enabled` set | comma list of model names to load |
| `ML_EMBED_MAX_ITEMS` / `ML_EMBED_MAX_CHARS` | 256 / 8000 | request ceilings |
| `ML_CLASSIFY_MAX_CHARS` / `ML_CLASSIFY_DEADLINE_MS` | 4000 / 250 | classifier bounds |
| `ML_MODEL_RETRY_MAX_S` | 300 | cap on the load-retry backoff |
| `ROUTER_HEAD_DIR` | `/app/heads` | where `head-*.npz/json` are read |
| `MAX_BATCH_WAIT_MS`, `ONNX_INTRA_OP_THREADS` | as today | unchanged |
