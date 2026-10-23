# Quickstart: validating the feature end to end

> **Superseded in part.** The trained routing head and `POST /classify` described here were deleted in spec 016 (`specs/016-decide-api/`); the app's router is rules plus a topic label (spec 015), asked through `POST /v1/decide`. This document is kept as the record of what was built and why.

Runnable scenarios that prove each story, in the order the PRs land. Each names what it proves (FR/SC) and what you should see.
Nothing here is a test body — the tests are in `tasks.md`. Commands assume `source .venv/bin/activate` and the repo root.

Prerequisites by tier: **A** = nothing (hermetic); **B** = Docker (testcontainers: Postgres/Redis/Qdrant/ml-service);
**C** = `make up` plus a native Ollama with `qwen2.5:3b`, `qwen2.5:7b`, `nomic-embed-text` (this is the maintainer's machine).

---

## 0. The suite is still green and still hermetic (tier A) — SC-005, SC-008, FR-042

```bash
make lint && make typecheck && make test
```

Expect: the same pass count as `main` plus the new hermetic tests; **no network, no containers**. Run it with the Docker daemon
stopped and with it running: the result must be identical.

## 1. Story 1 — judge a candidate against a baseline

### 1a. The pure parts (tier A) — SC-003, FR-008, FR-012

```bash
pytest tests/scripts/model_eval -q
python -m scripts.model_eval verify
```

Expect: every metric equals its hand-computed value; every control gets its expected verdict on synthetic inputs; `verify` reports
all committed dataset/baseline hashes intact, splits disjoint, every dataset licence on the allow-list.

### 1b. Fetch the public sets once (needs the network, once) — FR-009, FR-011

```bash
make model-eval-data
```

Expect: files land under `var/model_eval/public/`, each checked against the lock's sha256; nothing under `evals/` changes. Cut the
network and re-run any `model-eval` command: it must still work (SC-001's "offline once cached").

### 1c. Record the baselines (tier C) — FR-006

```bash
make model-eval-baseline KIND=embedding   CANDIDATE=nomic-embed-text@ollama
make model-eval-baseline KIND=reranker    CANDIDATE=ms-marco-minilm-l6
make model-eval-baseline KIND=screen      CANDIDATE=prompt-guard-2-22m
```

Expect: three files under `evals/baselines/` marked `"provisional": true` and a `PROVISIONAL` line in the output (the repo-own
sets are not yet reviewed by a person). The classifier baseline is recorded in step 3.

### 1d. Evaluate a candidate and read the verdict (tier C) — SC-001, SC-002, SC-003

```bash
make model-eval KIND=embedding CANDIDATE=bge-small-en-v1.5 VIA=inprocess
make model-eval KIND=embedding CANDIDATE=baseline-as-candidate     # the control
make model-eval KIND=embedding CANDIDATE=random-vectors            # the degenerate control
```

Expect: the first prints a report with `quality` intervals, `deltas` with verdicts, `efficiency`, `tradeoffs` (including
"dim 384 → …/million chunks; re-embedding every collection is required"). The second says `no_difference` on every metric; the third
`worse` on every metric. Run the first twice: identical quality numbers, p50 within 15%. Exit `5` and `status: invalid` if either
control misbehaves — that report must not be used.

## 2. Story 2 — embeddings used correctly, and a served ONNX model

### 2a. Defaults unchanged (tier A, then C) — SC-005

With no new settings: `make test` (above) passes; then, on the running stack, `make ingest` and ask a question. Compare the vectors
for a fixed text before and after the change: byte-identical (`python -m scripts.model_eval embed-probe "text"` prints a hash).

### 2b. Roles and prefixes (tier A) — FR-015..017

```bash
pytest tests/retrieval/test_embedding_roles.py -q
```

Expect: no caller of the old names; query/document functions apply the configured prefixes to the text sent to the client; the cache
uses the query form on both sides; prefixes default empty.

### 2c. The identity guard and re-embed (tier B) — SC-006, FR-018..020

```bash
make test-integration            # includes tests/integration/test_embedding_identity_real_qdrant.py
```

Expect the test (real Qdrant + Redis) to show: a collection built under identity A and read under B raises
`EmbeddingIdentityMismatch` on search and upsert; a legacy unaliased collection is adopted with a warning; `reembed` copies every
point (payload byte-equal, sparse vector kept), swaps atomically, and a search during the swap never errors; a source modified mid-run
aborts with exit 4 and the old collection still serves; the cache index name changes with the identity.
Record the Qdrant server version the test ran against in the PR.

### 2d. The embedding endpoint (tier A for the contract, B for the container) — FR-021..026, SC-007

```bash
pytest tests/ml_service -q                                  # in-process ASGI; the OpenAI SDK parses it, float and base64
make test-integration                                       # includes the container serving /v1/embeddings
curl -s localhost:8083/v1/models | jq '.data[].id'
curl -s localhost:8083/v1/embeddings -H 'content-type: application/json' \
     -d '{"model":"bge-small-en-v1.5","input":["hello","world"]}' | jq '.data|length, .usage'
```

Expect: `2`, a `usage` with token counts; `{"model":"nope"}` → `404 model_not_found`; 300 inputs → `400 batch_too_large`.
Stop one model (set a bad `file` in a scratch registry): `/health/models` shows it `failed` with `retry_in_s`, `/rerank` and
`/prompt-guard` still answer (FR-024).

### 2e. Point the gateway at it (tier C, manual) — FR-023, SC-007, the real-proxy verification

Follow `contracts/gateway-aliases.md` §2 steps 1–6 on a scratch Qdrant volume. Expect: after the proxy restart `GET /health/ready`
returns `warnings` naming the mismatch; a question gets no retrieved context (and `agent_embedding_identity_total{outcome="mismatch"}`
rises); after `make reembed COLLECTION_NAME=documents` the same question is answered with citations again; **zero source files changed**.

## 3. Story 3 — the routing seam

### 3a. Inert by default (tier A) — SC-008, FR-033

`make test` (step 0) — the existing suite passes untouched. Then `pytest tests/agent/graph/test_model_route.py -q`: with
`ROUTER_ENABLED=false` no `/classify` request is made and `agent()` calls are byte-identical (contract invariant 1).

### 3b. The classifier service and its failover (tier A, then B) — FR-027..030, SC-009

```bash
pytest tests/ml_service/test_classify_failover.py -q
curl -s localhost:8083/classify -H 'content-type: application/json' -d '{"task":"route","texts":["hi"]}' | jq
```

Expect: with no accepted head, `served_by: "none"`, `decision: "none"` — HTTP 200 (no head ships accepted; a head file with `"accepted": false` is refused the same way). Hermetically: a primary that
raises → `fallback` answers and says so; a primary that overruns the deadline → `fallback`; both down → `none`; a head built for another
model revision → refused.

### 3c. Build, judge and accept a head (tier C, operator) — FR-030, FR-038, SC-010

```bash
make router-label  JUDGE=chat-strong                       # needs Ollama with qwen2.5:3b and :7b
# (the head trainer, `make router-head-train`, was withdrawn: see tasks.md T070 and T076)
make model-eval KIND=classifier CANDIDATE=bge-small-en-v1.5
```

Expect: the report's `controls` show the head beating the `random` and `length` routers at equal escalation share, the cost–quality
curve with CPT values, calibration error; `heads/…json` has `"accepted": false` until a person sets it (the trainer refuses to).

### 3d. The seam end to end (tier C) — FR-031..037, SC-008..011

```bash
STRONG_CHAT_MODEL=chat-strong ROUTER_ENABLED=true ROUTER_THRESHOLD=0.5 make serve      # the dev chat-strong is local qwen2.5:7b
```

Send one prompt the head scores high and one it scores low. Expect (in the usage ledger / `GET /usage`): the first turn's rows name
the strong model, the second the local one; `agent_model_route_total` shows `strong` and `local`. Then:

- `docker compose stop ml-service` and send a turn → still answered by the local model, `outcome="classifier_error"`, no error to the caller.
- stop Ollama's 7B (set `chat-strong` to an unreachable `api_base`) and send a high-scoring turn → answered by the local model,
  `strong_failed_fell_back` counted, the ledger prices the **local** alias.
- send a mutating-tool prompt under each route → the same approval pause, the same tenant-scoped write, one row (`idempotent()`).

## 4. Governance — FR-040, FR-041

```bash
grep -n "never as the target model" .specify/memory/constitution.md CLAUDE.md README.md || echo "old rule gone"
head -3 .specify/memory/constitution.md; tail -2 .specify/memory/constitution.md   # version 1.2.0, a Sync Impact Report comment
pytest tests/core/test_doc_references.py -q                                         # the new docs name only files that exist
```

## What a pass looks like, in one line each

US1: a candidate and two controls produce one report each, controls agree, a re-run reproduces the numbers. US2: defaults unchanged;
a mismatch is refused and visible; a re-embed swaps without a mixed moment; the gateway reaches ml-service with no code change.
US3: off by default and identical; on, the advised turns use the strong model and are priced as it; every failure lands on the local
model; the approval and scoping behavior does not move.
