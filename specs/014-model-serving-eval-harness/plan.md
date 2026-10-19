# Implementation Plan: Model Serving Seams and an Evaluation Harness

**Branch**: `014-model-serving-eval-harness` (no git branch yet; work lands as the PR series in *Delivery*) | **Date**: 2026-10-17 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/014-model-serving-eval-harness/spec.md`

## Summary

Three connected changes, built in dependency order so each is judged by the one before it:

1. **An evaluation harness** (`scripts/model_eval/`, data in `evals/`): metrics implemented and hand-verified in-repo, paired-bootstrap
   verdicts, declared candidates, control candidates that prove the harness itself, frozen repo-own datasets from the four domains plus
   licence-pinned public sets, and recorded baselines for the models in use. It measures through the path the app uses.
2. **Embeddings done properly**: `embed_query` / `embed_document` replace the single ambiguous `embed_text`; configurable prefixes
   (default empty); an **embedding identity** carried in the collection name behind a Qdrant alias so a swap is atomic and two models'
   vectors can never share a collection; a `reembed` procedure; the answer cache namespaced by identity; and an OpenAI-compatible
   `/v1/embeddings` on ml-service (declared-model registry, existing micro-batcher), so the `embed` alias can point at an ONNX model
   with no app change.
3. **A routing seam**: ml-service `/classify` (bge-small-en-v1.5 primary, all-MiniLM-L6-v2 automatic fallback, each with its own
   trained head, bound to the exact model revision); a `chat-strong` LiteLLM alias present but inert; a `route_model` node that
   advises once per turn into `State.model_route`; `agent()` passes a per-call `model=` and prices the alias that served; every
   failure lands on today's local behavior. A constitution amendment (1.2.0) goes first.

`research.md` settles the open questions with evidence from this repository and from real runs; every claim it could not settle
without a real service is marked *to verify* (collected in its §F) and is a task, not an assumption.

## Technical Context

**Language/Version**: Python 3.13 (Dockerfile and CI), async-first for tool/store/client I/O.
**Primary Dependencies (existing)**: FastAPI, `langchain-openai` 0.3.x, `qdrant-client`, `redis` 8, `fastembed` 0.8 (sparse BM25 in the
app; the cross-encoder in ml-service), `onnxruntime` 1.29, `tokenizers`, `huggingface_hub`, `numpy` 2.5, `openai` 2.x.
**New dependencies (each with its reason, per the constitution):** `prometheus-client` in `docker/ml-service/requirements.txt` only (the
service has no metrics today; §B6); `pyarrow` in `requirements-dev.txt` only (public parquet datasets; §A2). **Nothing** is added to
`requirements.txt` or the lock file. `mteb`, `scikit-learn`, `datasets` are deliberately not used (§A2, §C1).
**Storage**: Qdrant (collections + aliases), Redis Stack (answer cache index), the git tree (`evals/`), and the untracked `var/model_eval/`.
**No Postgres schema change.**
**Testing**: pytest. Default tier stays hermetic (fake LLM, no services); new unit tests for metrics, ml-service logic (the package becomes
importable), the graph seam, roles/identity with a fake Qdrant client. `integration` tier (testcontainers) for the identity guard/re-embed
against real Qdrant+Redis and the ml-service container. Manual tier: the harness runs and the real-LiteLLM checks in `quickstart.md`.
**Target Platform**: Linux containers (ml-service: `cpus: 3`, `mem_limit: 2g` in prod); development on macOS with a native Ollama.
**Project Type**: a single Python application plus one auxiliary service image (`docker/ml-service`), plus a development tool.
**Performance Goals** (spec SC-001, SC-002, SC-009): a one-kind evaluation ≤ 10 min on the reference machine for the repo-own sets; quality
metrics reproducible exactly, median latency within 15%; the routing decision adds ≤ 50 ms p95 to turn start when healthy.
**Constraints**: defaults leave behavior unchanged (SC-005, SC-008); the core runs fully offline with the router unset; no content in logs
or metrics; every wait has a deadline (`ROUTER_TIMEOUT_SECONDS`, `ML_CLASSIFY_DEADLINE_MS`, the load-retry cap).
**Scale/Scope**: ~8 PRs, each ≤ ~1,000 hand-written lines (§E); datasets of a few hundred items per kind; two collections and one cache.

## Constitution Check

*GATE: passes before Phase 0 and is re-checked after Phase 1. One item is a deliberate amendment, not a violation (see the table).*

| Principle | Touched? | How the plan satisfies it | Status |
|---|---|---|---|
| **I** Fail-closed tenant isolation | yes | `/classify` is called with the user's message only — no `ctx`, tenant, principal or history (`graph-seam.md` step 2, test). Re-embed copies payload verbatim (tenant/owner fields untouched; a test diffs every payload). Query filters are unchanged and still pre-filters. Training/eval data never contains tenant text (`data-model.md` §2; scan test). The answer cache's tenant+principal tag filters are untouched. **`scripts/reembed.py` is an operator-only, whole-collection maintenance script that reads every tenant's points by design** (the same posture as the existing `tool_call_dedup_sweep` / `usage_events_sweep` scripts and `qdrant_store.delete_by_filter`'s own docstring); it is not a tool, is never reachable from the agent loop or the API (a test fails if `app/` imports it), and writes nothing but a verbatim copy. | PASS |
| **II** Mandatory approval | yes | Routing is advisory: capability tiers, `human_approval` and `tool_batch_guard` are not modified; invariant 5 runs the same mutating call under both routes. The router cannot reach a bypass because it writes only `model_route`. | PASS |
| **III** Fixed, typed tools | no tool added | No new tool, no change to any `args_schema`. | N/A |
| **IV** Exactly-once | yes | No new write path. A resumed turn keeps `model_route` (checkpointed) so it is not re-decided (invariant 6); `idempotent()` is unchanged. The re-embed is idempotent by construction (content-derived ids, reuse of an existing target, parity check). | PASS |
| **V** Bounded, observable failure | yes | Every wait has a deadline (router 0.5 s, service 250 ms, load-retry cap 300 s); every degrade path has a counter and the sustained ones an alert rule (`data-model.md` §10, `research.md` B6); the router never raises; the re-embed compensates by not swapping; logs carry metadata only. Broad `except Exception` sites carry `# noqa: BLE001 - reason`. | PASS |
| **VI** Untrusted content is data | yes | The classifier runs after `moderate_input`, so it never sees content moderation refused (graph edge + test); it treats the message as data and returns a number. `SYSTEM_PROMPT` stays a ctx-free constant — the route is **not** interpolated into it (invariant 4). | PASS |
| **VII** Test discipline | yes | The new turn-path `/classify` call gets an autouse mock in `tests/conftest.py`; a meta-test asserts the default suite makes no such call. Live pieces carry `integration`/`llm` markers and self-skip. Each bug-shaped risk has a regression test that fails without the fix. Claims about LiteLLM/Qdrant/SDK behavior are tasks that run the real thing. | PASS |
| **VIII** Why-first docs, honest gaps | yes | Patterns are added with their motivating failure (the missing prefixes; the same-dimension-different-space trap; the quantization trap) in `GRAPH_PATTERNS.md` 67–68 and `ENGINEERING_PATTERNS.md` 69; gaps are listed (spec *Assumptions*, `research.md`); the "Extending Further" entry for retrieval is updated. | PASS |
| **Architecture: LLM access** | yes | The rule says a cloud model "MAY be used only for eval and red-team grading roles, never as the target model or a runtime dependency". The stronger-alias seam is a runtime *target*. **Resolved by the amendment the requester chose to include (spec FR-040)**: PR 1 changes the rule through `/speckit-constitution`; the plan keeps the amendment narrow (an optional operator-configured alias; the core still fully offline; the alias defaults to a *local* model; a failure degrades to local). No PR that lets a turn use the alias merges before PR 1. | **AMENDED (PR 1)** |
| **Architecture: Configuration** | yes | Every new tunable is in `Settings` with a `.env.example` entry (`contracts/settings.md`); misconfiguration fails at import. | PASS |
| **Architecture: Schema changes / Checkpoint compatibility** | yes | No SQL. `State` gains an optional key read with `.get`; per the constitution and `graph/state.py`, an additive optional key and an appended node are not a break, with a test resuming a checkpoint that lacks it (`research.md` A6). | PASS |
| **Architecture: Dependencies** | yes | Two dev/aux-image dependencies, each with a reason comment; the lock file is not hand-edited. | PASS |
| **Architecture: Observability** | yes | New app counters go through `app/core/metrics.py`; ml-service gets its own `/metrics` and a scrape job. | PASS |
| **Gate: adds a `mutating`/`outward` tool?** | no | None. The re-embed is an operator script, not a tool; it is not reachable from the agent loop. | N/A |
| **Gate: adds a store or queue consumer?** | partly | Qdrant aliases and a Redis index name. Failure policy: a mismatch is absorbed on read / refused on write; metric `agent_embedding_identity_total`, alert `EmbeddingIdentityMismatch` (`reembed.md`). | PASS |

**Post-design re-check (after Phase 1):** unchanged. The design added two items worth stating: (1) readiness gains a `warnings` field
instead of failing, so an identity mismatch is visible without taking the API out of rotation (`reembed.md`); (2) the degrade-to-local is
in the app, not the gateway, so the ledger prices the model that served (`research.md` C3). Neither weakens a principle.

## Project Structure

### Documentation (this feature)

```text
specs/014-model-serving-eval-harness/
├── spec.md              # /speckit-specify
├── prior-art.md         # how benchmarks are built (research input)
├── plan.md              # this file
├── research.md          # Phase 0: decisions, rationale, alternatives, verification list
├── data-model.md        # Phase 1: declaration/manifest/baseline/report/identity/head/registry shapes
├── quickstart.md        # Phase 1: runnable validation per story
├── contracts/
│   ├── ml-service.md                    # endpoints: /v1/embeddings, /v1/models, /classify, /health/models, /metrics
│   ├── gateway-aliases.md               # chat-strong; embed → ml-service; what a target must provide
│   ├── settings.md                      # new Settings / .env.example entries
│   ├── model-eval.md                    # commands, exit codes, report rules
│   ├── model-eval.report.schema.json    # the report schema (validated in a hermetic test)
│   ├── graph-seam.md                    # State key, route_model, agent(), invariants
│   └── reembed.md                       # naming, identity guard, the procedure
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
app/
├── core/
│   ├── config.py                    # + the settings in contracts/settings.md; validation
│   ├── metrics.py                   # + agent_model_route_*, agent_embedding_identity_total
│   └── routing_advice.py            # NEW: the /classify client (deadline, never raises) — like moderation.py, imports nothing from agent/domains/api
├── retrieval/
│   ├── embeddings.py                # embed_query / embed_document / embed_documents / embedding_dim (old names deleted)
│   ├── embedding_identity.py        # NEW: EmbeddingIdentity, hash, adopt/verify, EmbeddingIdentityMismatch
│   ├── qdrant_store.py              # alias resolution (L-live), ensure_live, ensure_collection → L-H, check_identity on read/write
│   └── semantic_cache.py            # identity-namespaced index/prefix; embed_query on both sides
├── agent/
│   ├── cost/metering.py             # metered_invoke(..., invoke_model=None)
│   └── graph/
│       ├── state.py                 # State.model_route
│       ├── core.py                  # validate_input resets it
│       ├── model_route.py           # NEW: make_route_model_node
│       ├── agent_node.py            # per-call alias, STRONG_UNREACHABLE fallback
│       └── build.py                 # GraphProfile.include_router; node + edge
├── api/health.py, schemas.py        # readiness `warnings`
└── domains/ecorp/store.py           # embed_document

scripts/
├── model_eval/                      # NEW package (see research.md D1)
│   ├── __main__.py  metrics.py  stats.py  datasets.py  candidates.py  baselines.py  controls.py  report.py  hardware.py
│   ├── build/{retrieval,rerank,screen,route_prompts}.py   # builds the frozen repo-own datasets
│   ├── runners/{embedding,rerank,screen,classify}.py
│   ├── head.py  route_label.py
├── reembed.py                       # NEW
├── seed.py, index_skills.py         # use embed_document / embedding_dim

docker/
├── ml-service.Dockerfile            # COPY the package, keep CMD
└── ml-service/
    ├── main.py                      # shim: from ml_service.app import app
    ├── models.toml                  # NEW: the declared models
    ├── requirements.txt             # + prometheus-client
    ├── heads/                       # NEW: head-<model>.npz/.json (shipped or mounted)
    └── ml_service/                  # NEW package: batching, onnx_text, registry, embed, classify, rerank, guard, health, metrics, app

deploy/
├── litellm/litellm-config.yaml, litellm-config.prod.yaml   # + chat-strong
├── compose/docker-compose*.yml      # ml-service env, LLM_STRONG_* pass-through
└── env/prod.env.example             # + LLM_STRONG_*

evals/                               # NEW (data, not code)
├── README.md
├── candidates/{embedding,reranker,screen,classifier}.toml
├── datasets/<name>/<version>/{manifest.json,*.jsonl}   # repo-own, frozen
├── datasets/public.lock.json
└── baselines/<kind>/*.json

observability/prometheus/{prometheus.yml,prometheus.prod.yml,alerts.yml}   # + ml-service job, 4 alerts
tests/
├── scripts/model_eval/              # metrics, stats, datasets, controls, report schema, head
├── ml_service/                      # registry/failover/embed/classify contract (importable package)
├── retrieval/                       # roles, identity, qdrant alias (fake client)
├── agent/graph/                     # route node, seam invariants
├── integration/                     # identity + re-embed on real Qdrant/Redis; ml-service container
└── conftest.py                      # + autouse mock for routing_advice
pyproject.toml                       # pytest pythonpath += docker/ml-service; mypy path
Makefile                             # model-eval*, router-label, router-head-train, reembed
.specify/memory/constitution.md      # 1.2.0 (PR 1)
```

**Structure Decision.** The harness lives in `scripts/` (covered by lint and mypy, matches `python -m scripts.eval`) with *data* in a new
top-level `evals/` so datasets and baselines are reviewable as data and never mixed into code. The ml-service becomes an importable package
so its logic is unit-testable for the first time; `main.py` stays as a shim so the image's `CMD` and the compose healthchecks do not
change. App code stays in the existing packages; the only new `app/core` module (`routing_advice.py`) obeys the "imports nothing from
agent/domains/api" rule, and the only new graph module (`model_route.py`) is registered in `build.py` like every other node.

## Delivery (CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines, ~1,000 ceiling)

**As delivered.** The plan proposed nine PRs. At the maintainer's request the work was delivered as three, plus a closing change, so each is far
over the ~1,000-line ceiling and says so in its description (read each by commit). Order and dependencies are in `research.md` §E; every PR left
`make lint typecheck test` green and no default behaviour changed.

| PR | Contents (tasks) | What it added |
|---|---|---|
| #175 | the specification, plan, research, contracts, quickstart and this task list | `specs/014-model-serving-eval-harness/` |
| #177 | constitution 1.2.0 (T005-T007); the ML service as an importable package (T008-T018); the harness core, datasets, runners and provisional baselines (T019-T047); pattern 69 (T090) | `make model-eval*`, `evals/`, `scripts/model_eval/` |
| #176 | embedding roles, the identity guard, atomic re-embed, `/v1/embeddings` and the model registry (T048-T067) | `make reembed`, `app/retrieval/embedding_identity.py` |
| #178 | `/classify` with failover and heads (T068-T069, T073-T075, T077); the routing seam (T078-T087, T089); pattern 68 | `route_model`, the `chat-strong` alias, two alerts |
| closing change | the head trainer, labeller and classifier runner (T070-T072); the prefix A/B and the end-to-end walk-through run and recorded (T058, T088); docs and housekeeping (T091-T094) | `make router-label`, `make router-head-train`, `KIND=classifier`, the CLINC method-sanity check |

Setup tasks rode with the first PR that needed them. **94 tasks:** Setup 4, Foundational 14, US1 29, US2 20, US3 22, Polish 5. Open by design: T045, T076 and the T058 decision.

## Complexity Tracking

Complexity beyond the minimum, each with the simpler thing rejected:

| Addition | Why it is needed | Simpler alternative rejected because |
|---|---|---|
| Qdrant **alias** per collection (`L-live → L-H`) | FR-020 needs a swap with no moment of mixed identities and the old collection still serving on failure; an alias repoint is atomic server-side | In-place `update_vectors` mixes identities mid-run; rename does not exist; a config flip + restart means downtime and a window of writes to the wrong collection |
| Identity in the collection **name suffix** | Readable by every process with no extra store and no dependence on a server metadata feature | Postgres table: a schema change and a second source of truth; collection `metadata`: version-dependent, kept only as a diagnostic copy |
| ml-service **package split** (+ shim) | The service gains four responsibilities; one file would pass ~700 lines mixing them, and its logic could not be imported by a test or by the head trainer | Keeping `main.py`: untestable logic and a trainer/server code path that could drift (the quantization trap) |
| `prometheus-client` in the ml-service image | The service has no metrics and no scrape job; the routing/fallback state is only visible from inside it | `/health` polling by the app: no history, no alert without bespoke code; OTLP: no SDK or collector path for a bare container |
| `chat-strong` always present, defaulting to a **local** model | The seam stays config-only, the proxy cannot fail to start on an unset variable, the whole path runs in CI/dev with no third party | Optional/commented entry: not exercised, easy to rot; defaulting to a cloud model: breaks the offline guarantee |
| App-side degrade-to-local (not LiteLLM `fallbacks`) | The ledger must price the model that served; the app must count the degradation exactly | Gateway fallback: attributes a local answer to the strong alias's price and hides the event |
| Own metrics + bootstrap (no `mteb`) | `mteb` drags in torch and bypasses the app's path | Using it: violates the service images' no-torch posture and FR-013 |

No constitution principle is violated; the one rule that conflicts as written is resolved by the amendment (PR 1), not recorded as an exception.
