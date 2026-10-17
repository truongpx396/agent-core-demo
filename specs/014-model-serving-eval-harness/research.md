# Research: Model Serving Seams and an Evaluation Harness

Phase 0 of `/speckit-plan`, 2026-10-17. It settles the six open questions in `prior-art.md` §7 and every design choice the
plan depends on. Each entry is **Decision / Rationale / Alternatives**, and each fact is labelled:

- **Read** = read in this repository (path given). **Ran** = measured or exercised this session.
- **Hub** = read from a Hugging Face model/dataset card or file listing this session.
- **To verify** = a claim the implementation must prove with a real run before relying on it (Constitution VII). These become
  tasks (`tasks.md`), not assumptions.

---

## A. Questions carried over from `prior-art.md` §7

### A1. Licence of each public dataset, at its source (Q1)

**Decision.** Include these, pinned by Hub commit and per-file sha256, fetched on demand into the git-ignored `var/`
tree, never committed:

| Dataset | Use | Licence (Hub card, read 2026-10-17) | Format |
|---|---|---|---|
| `BeIR/scifact`, `BeIR/nfcorpus` (+ their `-qrels` repos) | retrieval, rerank sanity | CC BY-SA 4.0 | parquet / tsv |
| `deepset/prompt-injections` | screen | Apache-2.0 | parquet |
| `jackhhao/jailbreak-classification` | screen | Apache-2.0 | csv |
| `clinc/clinc_oos` (`plus`) | classifier method sanity incl. out-of-scope | CC BY 3.0 | parquet |

**Excluded, with the reason on the card:** `mteb/msmarco` (licence `other`: research-only terms), `mteb/scifact` and
`mteb/fiqa` (licence `unknown`: use the `BeIR` originals whose card states one), `xTRam1/safe-guard-prompt-injection` (no
licence), `allenai/wildjailbreak` (gated), `PolyAI/banking77` (CC BY 4.0, but the repo ships only a loader *script*, and
running a dataset's script is running downloaded code — excluded rather than trusted).

**Rationale.** The harness refuses an unrecorded licence (FR-009), so the allow-list is data in `public.lock.json`, and a test
asserts every entry has a licence from a small allowed set. CC BY-SA applies to *redistributing adaptations*; the harness
caches and reads, and redistributes nothing (the cache is untracked), which is stated in the dataset README.

**Caveat (disclosed).** A Hub card's licence is the publisher's claim and can differ from the original paper's repository.
The check is "what the pinned source states", recorded with the date.

**Alternatives.** Pull MTEB's mirrors (rejected above); `datasets` library (heavy, and executes dataset scripts).

### A2. Depend on `mteb`? (Q2)

**Decision.** No. The harness implements its own metrics (`scripts/model_eval/metrics.py`) and checks them against
**hand-computed worked examples**, not against another library.

**Rationale.** `mteb` pulls `sentence-transformers` and therefore `torch` (the thing `docker/ml-service/main.py` documents
avoiding at 2 GB+), and it runs models through its own wrappers — not through the app's path, which is the point of FR-013.
The metrics needed (recall@k, MRR, NDCG, ROC-AUC, TPR at fixed FPR, accuracy, macro-F1, ECE, a bootstrap) are each a few lines
of numpy; the risk is a *wrong* implementation, which hand-computed expectations and the control candidates (FR-008) catch.

**New dev dependency, with its reason (Constitution: every non-obvious pin carries one):** `pyarrow` in
`requirements-dev.txt` only, to read the public parquet files. **Ran:** `pyarrow` is not installed today; `scipy` is, as a
transitive dependency, and is deliberately not relied on. Nothing is added to `requirements.txt` or the lock file.

**Alternatives.** `mteb` (above). `pytrec_eval` (C extension; adds a build step for ~30 lines of arithmetic).

### A3. Generating the repository's own question sets (Q3)

**Decision.** Generate with the **local** model already pulled for this repo (`qwen2.5:7b`, **Ran:** present on the
maintainer's Ollama), then filter automatically, then mark every item `reviewed: false` until a person flips it.

- Automatic filters: drop a question that shares a long verbatim span with its source chunk (a lexical-overlap cap, so the set
  tests meaning rather than string match), drop duplicates, drop a question answerable from a different chunk by the same
  rule (kept as an adversarial item instead when two chunks genuinely both answer it).
- A baseline recorded on a set with unreviewed items is written with `provisional: true` and every report prints a
  `PROVISIONAL` banner naming the unreviewed share. Re-recording after review clears it. **I (the implementer) cannot review
  these; the human-review task is explicit in `tasks.md` and is not marked done by code.**

**Rationale.** The constitution allows a cloud model for evaluation roles only, but nothing here needs one, and keeping the
whole harness offline is FR-011. A 7B model writes serviceable questions; its weakness (too-easy, copy-the-chunk questions) is
what the overlap filter targets. Provenance (model id, prompt hash, date, filter settings) is recorded per dataset.

**Alternatives.** A cloud model as generator (permitted, but breaks the offline guarantee and adds a vendor); hand-writing
everything (best quality, but ~hundreds of items for four domains is the reason sets stay small and unreviewed otherwise).

### A4. Bootstrap, and when a one-point difference is resolvable (Q4)

**Decision.** Percentile bootstrap over **queries** (the unit of independence), 2,000 resamples, fixed seed
(`20261017`), 95% interval. A comparison uses the **paired** bootstrap on per-query metric differences (candidate − baseline
on the same queries), which is far tighter than comparing two independent intervals. Verdict: *better*/*worse* if the interval
of the paired difference excludes 0, else *inconclusive*. The "examples needed" figure is the normal-approximation
`n · (half_width / |delta|)²`, labelled an estimate.

**Rationale.** Standard, deterministic with a fixed seed (SC-002), and needs only numpy. Resampling queries rather than
(query, passage) pairs respects that one query's several relevant passages are not independent.

**Not claimed.** I give no smallest-set-size number here; it depends on per-query variance and is produced by the harness for
each comparison, which is the honest form of the answer.

### A5. How retrieval degrades today (Q5)

**Read.** `app/retrieval/qdrant_store.py::hybrid_search` has two internal degrade paths (sparse leg → dense-only,
`agent_retrieval_degraded_total{stage="sparse"}`; reranker → RRF order, `{stage="rerank"}`) and **none for the dense embedding or
a missing collection**: an exception propagates. The callers absorb it: `graph/retrieval.py::retrieve_context` catches
everything, returns an empty context and increments `agent_context_retrieval_degraded_total`; a `search_docs` tool call
surfaces the error through the `ToolNode` error handler; `skill_search` catches it and answers "no skills catalog".

**Decision.** An embedding-identity mismatch is a typed exception (`EmbeddingIdentityMismatch`) raised from the retrieval
functions on a **read** of a mismatched collection. It degrades exactly as an outage does (the three absorbing callers above,
unchanged), plus its own counter so it is distinguishable. **Writes** are refused by the same exception and are *not*
absorbed (an ingest job fails visibly, as `IngestRefused` does for a bad ctx).

**Rationale.** FR-019 asked for "degrade as an outage does today"; this is literally that mechanism. No new degrade path.

### A6. Pricing an unpriced alias, and the per-turn model choice (Q6)

**Read.** `app/agent/cost/pricing.py::refuse_unpriced(alias)` is true when `UNPRICED_MODEL_POLICY == "block"` and the alias has
no price; prices come from LiteLLM `/model/info` (a self-hosted model is priced by `model_info` in the LiteLLM config).
`run_subagent` already applies the same refusal to a specialist's own `model:` before any model work. COST_GOVERNANCE.md
states "unknown is not free".

**Decision.** The router asks `refuse_unpriced(STRONG_CHAT_MODEL)` before escalating; if refused it routes **local** and counts
`agent_model_route_total{outcome="strong_unpriced"}`. It never refuses the *turn* (advisory, FR-035). Under any other policy
the escalation proceeds and the existing `agent_unpriced_usage_total` counts the call. An exhausted allowance already
refuses the turn at entry (`runtime/core.py::allowance_refusal`) before the graph starts, so there is no separate "caps not
exhausted" test at routing time; FR-033 is reworded accordingly.

**Per-turn choice and `STATE_SCHEMA_VERSION`.** **Read:** `State` is a `TypedDict`; the schema version is bumped only for "a
renamed/removed State key, or a removed/reordered node a paused thread might resume into" (`graph/state.py`); a new node
appended is explicitly not a bump. A new optional key `model_route` is read with `state.get(...)`, so a checkpoint written before
it simply has no route and runs local. **Decision: no bump**, backed by a test that resumes a state lacking the key. The
choice lives in `State` (checkpointed), so approval pause/resume and crash continuation inherit it (FR-034) with no extra code,
and `validate_input` resets it to `None` each new turn (a resume skips that node, as `approved`/`cancelled` rely on).

---

## B. Embedding design

### B1. Query vs document roles (FR-015..017)

**Read.** `embed_text` serves both roles at: `qdrant_store.hybrid_search` (query), `semantic_cache.get/set` (question vs earlier
question), `domains/ecorp/store.py` ×2 (document: notes, memories), `scripts/index_skills.py` (document, plus the dimension
probe), `scripts/seed.py` (probe); `embed_texts` serves `ingestion/ingestor.py` (document).

**Decision.** Replace the two functions with `embed_query`, `embed_document`, `embed_documents` and a cached
`embedding_dim()` probe. The old names are **deleted** (not deprecated) so a stray caller is an `ImportError`, and an AST test
over `app/` and `scripts/` fails if either old name reappears or if any call to the underlying client bypasses the role
functions. The semantic cache uses `embed_query` on both sides (symmetric, FR-017).

**Prefix settings.** `EMBED_QUERY_PREFIX` and `EMBED_DOCUMENT_PREFIX`, default `""`. `.env.example` documents nomic's values
(`search_query: ` / `search_document: `) beside the re-embed requirement. **Hub (nomic-embed-text-v1.5 card):** the prompt "must
include a task instruction prefix". **Not measured:** how much retrieval improves; the card does not say what happens without
them. That is the first thing the harness quantifies (Story 1 → Story 2 A/B), which is why the default stays empty.

### B2. Embedding identity and the swap mechanism (FR-018..020)

**Problem.** Same dimension does not mean same space: nomic (768) and gte-modernbert-base (768) are interchangeable to Qdrant
and to any size check, and wrong to every search.

**Decision.**

- **Identity** = `(alias, resolved model, dimension, query prefix, document prefix, EMBED_MODEL_REVISION)`, hashed to 8 hex
  chars. "Resolved model" is `model_resolver.resolve_model(EMBED_MODEL)` (LiteLLM `/model/info`, **Read**: cached for the process
  life on success). `EMBED_MODEL_REVISION` is an optional free-text setting an operator bumps when they re-pull a model in place,
  the one change the resolver cannot see (disclosed gap otherwise).
- **Physical collections are named `<name>-<hash8>`; the app reads and writes through an alias `<name>-live`.**
  Qdrant alias updates are atomic, so a re-embed builds the new physical collection, verifies it, and repoints the alias in one
  request. A failure before the repoint leaves the old collection serving (FR-020).
- **The check** compares the configured identity's hash with the suffix of the alias's current target (cached ~10 s so a search
  costs no extra round trip, and a swap is picked up within that time). It needs no collection-metadata feature.
- **Legacy.** An existing deployment has a physical collection named exactly `<name>` and no alias. On first use `ensure_live`
  creates `<name>-live → <name>` (atomic, non-destructive) and treats it as **adopted**: the configured identity is *assumed* to
  match, with a warning and `agent_embedding_identity_total{outcome="adopted"}`. This keeps "defaults unchanged" working with
  no operator step. It is also the one place the guard is weaker than elsewhere: if an operator changes the model *and* upgrades
  in one step, adoption cannot tell. **Disclosed gap**; a re-embed replaces the legacy collection with a verifiable one.
- **Readiness.** Per **Read** (`api/health.py`, `ReadinessResponse`), `/health/ready` is `checks: dict[str, bool]` and any false
  makes the API return 503. An identity mismatch must be *visible* but must not take the API out of rotation (retrieval degrades;
  turns still run), so it is reported in a new `warnings` field of the response and **does not** set the status to degraded.
- **Semantic cache.** Index name and key prefix gain the identity hash (`idx:semantic_cache:<hash8>`, `cache:<hash8>:`), so a
  new identity gets a fresh, correctly-dimensioned index and the old entries are simply never read and age out by the existing
  TTL. This satisfies "answers cached under the old identity are never served" without a flush.

**Verified/To verify.**
**Read:** `qdrant-client` exposes `update_collection_aliases` and `CreateAlias`; the installed client's `CreateCollection` has a
`metadata` field. **To verify (integration test, real Qdrant):** an alias can be repointed atomically while a search runs; a
point written to the alias lands in the target; `scroll` with vectors returns the named sparse vector (so a re-embed copies
sparse vectors rather than recomputing them). The server image is `qdrant/qdrant:latest` (**Read**), so the test records the
version it ran against.

**Alternatives.** In-place `update_vectors` (rejected: a moment of mixed vectors, FR-020). Rename (Qdrant cannot). Storing the
identity in Postgres (a schema change and a second source of truth for something the collection name can carry). Flip a config
value and restart (downtime and a window of writes to the wrong collection).

### B3. The re-embed procedure (FR-020)

**Read:** every point stores the exact embedded text in `payload["text"]` — document chunks (`ingestor.py`: `text` is the child
chunk that was embedded; `parent_text` is separate), notes and memories (`ecorp/store.py`: `text`, `content`), skills
(`index_skills.py`: `"name: description"`). So **no original upload is needed** and the assumption in the spec holds for every
collection that exists today. Point ids are content-derived for documents (`_content_point_id`), so they survive a copy.

**Decision.** `scripts/reembed.py <collection>` (also `make reembed`): create `<name>-<newhash>`; scroll the live collection in
pages **with** payload and named vectors; re-embed `payload["text"]` with the document role in the ingest batch size; write
payload, the **existing sparse vector** and the new dense vector with the same id; verify id-set parity with the source and a
sample of 100 neighbours; repoint the alias; leave the old physical collection in place (an operator deletes it after a bake
period; documented). Idempotent: if the target already exists with parity it is reused. Aborts without repointing if the source
count changed during the run (the **disclosed** residual: a write in the milliseconds between the final parity check and the
repoint). Tenant/owner scoping is untouched because payload is copied verbatim (Principle I), and a test asserts no payload
field differs.

**Not done:** per-tenant partial re-embed (the unit is a collection). Re-embedding the answer cache is unnecessary (B2).

### B4. ml-service `/v1/embeddings` (FR-021..026)

**Read.** `openai` 2.54 and `langchain-openai` 0.3.35 are installed. **To verify:** the OpenAI SDK requests
`encoding_format="base64"` when none is given and decodes it; LiteLLM may forward it. **Decision:** support both `float` and
`base64` (little-endian float32), reject `dimensions` with a clear 400 (the served models are not trained for truncation; nomic
and the arctic family are, and are not in the initial list), and prove the response parses with the **official SDK pointed at
the ASGI app in-process** (a hermetic test), plus once against a real LiteLLM (manual, in `quickstart.md`).

**Served models** are declared in `docker/ml-service/models.toml` (name, HF repo, file, revision, pooling, normalise,
max length, prefixes the *model* needs, licence). Initial entries: `bge-small-en-v1.5`, `gte-modernbert-base-int8`,
`all-MiniLM-L6-v2` (the classifier fallback shares its loaded session). **Hub:** all three licences are MIT/Apache-2.0;
`BAAI/bge-small-en-v1.5` ships only an fp32 `onnx/model.onnx` (133 MB), `all-MiniLM-L6-v2` several (fp32 90 MB, int8 variants
23 MB), `gte-modernbert-base` `onnx/model_int8.onnx` (150 MB). The registry names a file explicitly; quantized bge-small is a
*candidate* for the harness to judge, not an assumption.

**Correction (measured 2026-10-17, M4 Pro, the same 200 real 600-char chunks, avg 259 tokens each).** The first benchmark in
this work used fastembed's **int8** variants and reported 47 ms/chunk for bge-small; that figure was pessimistic and should not be
used. Re-measured like for like: raw-onnxruntime **fp32** bge-small **17.1 ms/chunk** (3 and 12 threads), all-MiniLM-L6-v2
**7.4–8.6**, fastembed int8 bge-small 21.5 (3 threads) / 35.8 (12 threads, noisy), the current model's own fp32 ONNX
(nomic-embed-text-v1.5) **50–73**, and the current Ollama-on-Metal setup **13.6–15.1**. So: on this ARM CPU int8 was *slower*
than fp32 (the quantized path is not a free win here, and prod is likely x86 where it may differ — a candidate for the harness, run
on the target CPU, not an assumption); a small ONNX model matches today's Ollama throughput, but the **same model**
(nomic) is 3–5× slower on CPU than on Metal. Query latency is not the constraint: bge-small 2.5–2.8 ms p50 and MiniLM 1.0–1.6 ms
for a short user message at 1, 3 or 12 threads. Process memory after loading the fp32 file: bge-small +245 MB (+335–364 MB after
running), MiniLM +177–201 MB (+301–319 MB) — larger than the file, which matters for the container's `mem_limit`. The default registry
entries therefore name the fp32 files, and `models.toml` keeps int8 variants as separate, harness-judged candidates.

**Prefixes live in the app, not the server.** The role (query vs document) is known only to the app; the OpenAI request shape has
no role. The app adds `EMBED_*_PREFIX` text before sending. The registry's own "prefixes the model needs" field is documentation
and is echoed by `/v1/models` so the maintainer can copy the right values into `.env`. (A server-side default would silently
double-prefix.)

**Concurrency.** Reuse the existing `_MicroBatcher` (**Read**, `main.py`): one batcher per model, one pass at a time.

**Resource ceiling.** **Read:** prod gives ml-service `cpus: "3"`, `mem_limit: 2g`, and the dev image caches models in a volume.
Adding three small models is ~0.4 GB of weights on disk and well under the limit in memory; a start-up test asserts the
configured set loads within the container's limit.

### B5. Splitting `docker/ml-service/main.py`

**Read:** one 300-line file, `Dockerfile` copies only `main.py`, no test imports it (the image is exercised through
`tests/containers.py::ensure_ml_service` only). Adding two endpoints, a registry, health states and metrics would roughly
double it and mix four responsibilities (CLAUDE.md: ~500 lines is the prompt to split).

**Decision.** A package `docker/ml-service/ml_service/` — `batching.py` (the `_MicroBatcher`, moved verbatim), `onnx_text.py`
(tokenise/pad/run/pool, shared by guard, embedder and classifier), `registry.py` (the declared models and their load state with
bounded retry), `embed.py`, `classify.py`, `rerank.py`, `guard.py`, `health.py`, `metrics.py`, `app.py` — and a one-line
`main.py` (`from ml_service.app import app`) so `uvicorn main:app` and the Dockerfile `CMD` are unchanged. The refactor lands
**first, behavior-identical**, proved by the existing container tests plus new hermetic unit tests (which are possible for the first
time because the code becomes importable; `docker/ml-service` is added to pytest's `pythonpath`).

### B6. Metrics and alerts for a service that has neither

**Read:** ml-service exposes no metrics, and `observability/prometheus/prometheus.yml` has no job for it; the app counts what it
*sees* (`agent_moderation_ml_degraded_total`, `agent_retrieval_degraded_total{stage="rerank"}`).

**Decision.** Two layers. **App-side** counters (new, in `app/core/metrics.py`) for what the app does with the service: the
routing decision outcomes, identity checks. **Service-side** `/metrics` via `prometheus-client` (one small pure-Python
dependency, added to `docker/ml-service/requirements.txt` with its reason), scraped by a new `ml-service` job in
`prometheus.yml` and `prometheus.prod.yml`, for what only the service knows: per-model load state, the classifier's primary/
fallback state, embedding request/error/latency/batch-size. Alert rules: `MlServiceModelDown` (a declared model not `ready` for
5 m), `RouterClassifierOnFallback` (fallback active for 15 m), `EmbeddingIdentityMismatch` (app counter > 0 for 5 m),
`ModelRouteStrongFailing` (escalated turns falling back to local for 10 m).

**Alternatives.** Report state only through `/health` and let the app poll it (no history, no alert without bespoke code);
OTLP from ml-service (it has no OTel SDK, and the collector path exists for the app, not for a bare container).

---

## C. Classification and routing design

### C1. Classifier models, heads and the shared embedder (FR-027..030)

**Decision.** Primary `BAAI/bge-small-en-v1.5`, fallback `sentence-transformers/all-MiniLM-L6-v2`, both through the same generic
raw-onnxruntime embedder as the embedding endpoint (`onnx_text.py`), so there is **one** code path for pooling, truncation and
revision. A head is `sigmoid(w·x + b)` over the L2-normalised embedding: `head-<model>.npz` + `head-<model>.json` (provenance:
model repo, **commit sha**, dataset version + hash, held-out metrics, trained-at, threshold curve). The service refuses a head
whose recorded sha differs from the loaded model's. **Revision source:** the Hub cache stores files under
`snapshots/<commit-sha>/`, so the sha is the directory name of the downloaded file (**To verify** at implementation, with the
exact `hf_hub_download` return path).

**Training** is numpy-only (`scripts/model_eval/head.py`: L2-regularised logistic regression, fixed iteration count, fixed
seed) — no scikit-learn, **Ran:** it is not installed and a 40-line trainer is easier to make deterministic than a dependency.
The trainer embeds with the **same module the service uses**, imported from `docker/ml-service` via the pythonpath entry, so a
head can never be trained on one pipeline and served on another (the quantization trap in `prior-art.md` §1).

**Why a fallback needs its own head (Spec edge case):** bge-small and MiniLM are both 384-d but different spaces. **Ran
earlier:** both load and run in this environment.

### C2. Failover semantics (FR-029)

State machine per classifier model: `loading → ready → failed(backoff)`. A request uses the primary if `ready`; if it is not
ready, errors, or exceeds its deadline (default 250 ms inside the service, shorter than the app's own 500 ms), the fallback
answers and the response carries `served_by: "fallback"`. A failed model is retried with exponential backoff (5 s → 5 min cap),
not on every request. Both unavailable → HTTP 200 with `decision: "none"` ("no advice"), never a 5xx the app has to interpret.

### C3. Where the decision lives in the graph (FR-031, FR-034, FR-035)

**Read:** the graph is built **once** with one tool-bound client; `agent()` calls `metering.metered_invoke(llm, messages,
config=config, kind=kind, model_alias=model_alias)`, which does `llm.ainvoke(messages, **gateway.identity_from_config(config))`
and prices/records by the `model_alias` it is given. Compaction and follow-ups reuse the same client.

**Verified (Ran, installed source):** `langchain_openai ... _get_request_payload` builds `payload = {**self._default_params,
**kwargs}`, so a per-call `model=` keyword overrides the client's model. **Decision:** no second client. `agent()` reads
`state.get("model_route")`; for `"strong"` it passes `model=STRONG_CHAT_MODEL` and `model_alias=STRONG_CHAT_MODEL` to
`metered_invoke` (so the ledger and the per-turn ceiling price the model that served). Compaction and follow-ups stay on the
local alias (they are not "the answer"; documented). A new node `route_model` runs after the cache miss and compaction,
before `retrieve_context`, so it never runs for a refused input, a cache hit, or an approval resume. It is a flag on
`GraphProfile` (on for the parent graph, off for `SUBAGENT_PROFILE`).

**Degrade to local (FR-037).** In the app, not the gateway: if the strong call raises an error classed as "never reached a
model" (connection/timeout errors and HTTP 502/503/504 before a response; **not** `provider_budget_exceeded`, which
`AGENT_RETRY_POLICY` deliberately never retries, and not 4xx), `agent()` counts
`agent_model_route_total{outcome="strong_failed_fell_back"}` and repeats that one call on the local alias. **Why not
LiteLLM `fallbacks`:** the ledger would attribute the call to the strong alias's price although the local model served it, and
the app could not count the degradation precisely. **To verify:** a real-LiteLLM run of a forced failure, and that the error
classes the SDK raises match the list (unit tests with fakes cover each class; the manual run covers the real path).

### C4. The strong alias's default (FR-032, FR-040)

**Decision.** `chat-strong` is **always present** in both gateway configs and resolves, by default, to a model that is not a
third party: in the dev config `ollama_chat/qwen2.5:7b` (**Ran:** already pulled), in the prod config the value of
`LLM_STRONG_CHAT_MODEL` defaulting (in compose) to `LLM_CHAT_MODEL`. The *app* switch is separate and defaults off
(`STRONG_CHAT_MODEL=""`, `ROUTER_ENABLED=false`). An operator who wants a cloud model edits one LiteLLM entry (documented
in `contracts/gateway-aliases.md`).

**Rationale.** (1) Config-only attach, no startup failure from an unset variable (**Read:** the prod config uses LiteLLM's
`os.environ/VAR` substitution, which cannot be conditionally absent). (2) The seam is exercised end to end — in CI's
live tier and on a laptop — against a *local* bigger model, with no data leaving the machine and no key. (3) It makes the
constitution amendment narrow: the only thing permitted is that an operator *may* repoint one alias.

**The contract a target must meet** (documented, tested with the stand-in): OpenAI chat-completions with streaming and tool
calls, usage in the final chunk (`stream_usage`), and a LiteLLM `model_info` price if `UNPRICED_MODEL_POLICY=block`.

### C5. Labels for a head (FR-030, FR-039)

A head needs a per-prompt outcome from both models. **Decision.** `scripts/model_eval/route_label.py` runs the committed
`route-domains` prompts through the local alias and the strong alias, has a **judge alias** (an operator-configured model, which
may be the strong one; the constitution permits cloud use for evaluation roles) score each answer 0–1 against a short rubric,
and writes `outcomes/<strong-model-id>.jsonl`. Label = `needs_stronger` when `strong − local ≥ margin` (default 0.2).
Outcomes are never committed unless the operator chooses; the hermetic tests use synthetic outcomes. This is run only on request
and only for prompts authored for this repo (FR-039). **The first head's quality is bounded by the prompt set** (disclosed).

### C6. What "routing" does not decide

Not the tool set, not capability tiers, not approvals, not the system prompt (Principle VI: it remains a ctx-free constant; the
route is *not* interpolated into it, and a test asserts the system prompt is byte-identical under both routes). The only things
that vary with the route are the `model=` of the agent node's calls and the alias they are priced under.

---

## D. Harness design

### D1. Shape and location

`scripts/model_eval/` (Python package, covered by `make lint` and `make typecheck`, run as `python -m scripts.model_eval`,
matching `python -m scripts.eval`); data in `evals/` (`datasets/`, `baselines/`, `candidates/`, `README.md`). Targets are named
`model-eval*` so they are never confused with the golden-dataset `make eval`.

Modules: `metrics.py` (pure) · `stats.py` (bootstrap, verdicts) · `datasets.py` (manifests, verification, public fetch, overlap
check) · `candidates.py` (TOML declarations) · `baselines.py` (read/write the immutable baseline records) · `build/` (the builders of the frozen repo-own datasets) · `runners/{embedding,rerank,screen,classify}.py` (one measured path each) ·
`controls.py` · `report.py` (schema, render, compare) · `hardware.py` · `head.py` (train/export) · `route_label.py` · `__main__.py`.

### D2. Metrics (FR-003)

All numpy, all with a worked hand-computed expected value in `tests/scripts/model_eval/`:
recall@k (fraction of a query's relevant items in the top k, averaged over queries with ≥1 relevant); MRR@k; NDCG@k with graded
gains and `log2(rank+1)` discount normalised by the ideal; ROC-AUC (rank-sum with tie correction); TPR at FPR ≤ 1% (highest
true-positive rate over thresholds whose false-positive rate does not exceed the cap, so it is a conservative reading); accuracy;
macro-F1 (classes with no support excluded and counted); ECE (15 equal-width bins, reported with the bin count); cost–quality
curve (escalate the top-`p` share by score; **PGR(p) = (Q(p) − Q_local) / (Q_strong − Q_local)** per RouteLLM, with CPT(x%) the
smallest `p` reaching `x`, and APGR the mean over a fixed grid); random-router PGR is `p` in expectation and also sampled; the
prompt-length router sorts by token count.

### D3. Measurement paths (FR-013)

| Kind | Primary path (what the app does) | Optional in-process path |
|---|---|---|
| embedding | the gateway `embed` alias through `embeddings.embed_query/embed_document` (so prefixes apply) | the generic ONNX embedder, for a model not yet served |
| reranker | `POST ml-service /rerank` (`embeddings.rerank`) | `fastembed` cross-encoder |
| screen | `POST ml-service /prompt-guard` + the app's `moderation.screen` decision | — |
| classifier | `POST ml-service /classify` | the embedder + a head file |

The report's `path` field says which. Latency is per call through that path; ms/chunk uses the app's ingest batch size
(`EMBED_BATCH_SIZE`).

### D4. Hardware profile and resident memory (FR-002, FR-004)

Profile = CPU brand string, physical/logical cores, the ONNX intra-op thread count, `onnxruntime` and `tokenizers` versions,
OS; id = 8-hex hash of those. Latency baselines key on the id. Resident memory = peak RSS delta across model load
(`resource.getrusage`, unit normalised per platform); documented as approximate and as the in-process figure (the container's
number differs).

### D5. Controls (FR-008)

Embedding: *identity* (the baseline under another name → "no difference") and *random vectors* (seeded). Reranker: *identity* and
*random scores*. Screen: *identity* and *always-benign* and *random*. Classifier: *identity*, *constant*, and the *random* and
*length* routers. A hermetic test runs each control through the real metric/compare code on synthetic inputs and asserts its
verdict; the integration run asserts them on the real datasets.

---

## E. Delivery boundaries (CLAUDE.md: ≤ ~400 hand-written lines per PR, ~1,000 ceiling)

Datasets, baselines and lock files are data and do not count; tests do count toward review size but not toward the cap
(CLAUDE.md: files). Estimated hand-written lines in parentheses.

| PR | Contents | Stories |
|---|---|---|
| 1 | Constitution amendment to 1.2.0 (via `/speckit-constitution`), corrected restatements | US3 prerequisite (~60) |
| 2 | Harness core: `metrics`, `stats`, `report`, `hardware`, `candidates`, `controls`, hermetic tests, `make` targets, `evals/README` | US1 (~700) |
| 3 | Harness data + runners: dataset manifests/fetch/verify, repo-own sets, runners, baselines (provisional) | US1 (~600 + data) |
| 4 | ml-service split into a package + `/health/models` + `/metrics`, behavior-identical | US2/US3 enabler (~500, mostly moved) |
| 5 | Embedding roles, identity, aliases, semantic-cache namespacing, `reembed`, readiness warnings, settings, alerts | US2 (~800) |
| 6 | ml-service `/v1/embeddings`, registry, SDK-parity tests, gateway doc | US2 (~450) |
| 7 | ml-service `/classify`, failover, heads, `head.py` trainer, `route_label.py` | US3 (~650) |
| 8 | App seam: settings, `route_model` node, `State.model_route`, per-call model, degrade-to-local, `chat-strong`, metrics, alerts, docs, patterns | US3 (~600) |

A ninth, small closing-docs PR (tasks T090–T094) follows PR 8. PRs 1, 2 and 4 are independent and can start in parallel; 3 follows 2; 5 follows 3 (it wants the A/B); 6 follows 4; 7 follows 4
and 3; 8 follows 1, 7 and 5 (it shares `chat`/`embed` config).

## F. Verification tasks this research creates (all in `tasks.md`)

1. Real Qdrant: alias repoint is atomic under a concurrent search; write-through-alias; scroll returns sparse vectors; record the
   server version.
2. Real LiteLLM: `embed` alias → `openai/<model>` with `api_base` = ml-service returns vectors the app accepts, with and without
   `encoding_format=base64`; forced strong-alias failure produces the error classes the degrade list names.
3. The official `openai` SDK parses `/v1/embeddings` in-process (hermetic).
4. The Hub cache path yields the commit sha used to bind a head.
5. A State checkpoint without `model_route` resumes and runs local.
6. The system prompt is byte-identical under both routes; the same tool call has identical approval/scoping under both.
7. Parity of served vectors with the model's reference implementation on a sample.
8. Controls produce the expected verdicts on the real datasets.
