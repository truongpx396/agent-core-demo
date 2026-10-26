# CLAUDE.md

A production-shaped, fully local LangGraph RAG agent: LiteLLM proxy → Ollama, Qdrant, Postgres,
Redis Streams, Langfuse, FastAPI. Three example domains (support, ops, sales) run on one
unmodified graph. Python 3.13.

Read these before changing behavior; they hold the reasoning this file deliberately omits:
- `.specify/memory/constitution.md` — the non-negotiable rules. Principles I, II and IV
  (tenant isolation, mandatory approval, exactly-once side effects) are NON-NEGOTIABLE.
- `GRAPH_PATTERNS.md` — the graph's numbered patterns (1-69 so far; 51 live there), each with the real bug
  that motivated it. "Extending Further" is the honest list of what is and isn't built. A pattern that is
  not about the graph moves out, leaves its number in a table here, and the number is never reused:
  the spend meter and caps are in `COST_GOVERNANCE.md`, three repository lessons in `ENGINEERING_PATTERNS.md`,
  and the billing package keeps its own (`packages/billing/docs/PATTERNS.md`, see the constitution's
  "Self-contained packages").
- `README.md` — architecture, make targets, testing tiers. `WORKER_CONCURRENCY.md` — worker sizing.
  `BILLING_INTEGRATION.md` — the app's side of the billing seam; everything else about billing (patterns,
  runbook, roadmap, specs) is in `packages/billing/`.

## Commands

The Makefile assumes an activated venv: `source .venv/bin/activate`.

```bash
make lint            # ruff check .          (CI gate)
make typecheck       # mypy over app/ scripts/ (CI gate)
make test            # pytest -n auto -q — hermetic, no services needed (CI gate); then the billing package's own tests
make test-billing-package   # only the package's hermetic tests, from its directory (the app is not on the path)
make billing-migrate        # apply the billing package's schema scripts to an existing database (billing-migrate-status: read-only)
pytest tests/core/test_tool_idempotency.py::test_name -q   # one test; marked tiers are skipped by default
make test-integration  # real Postgres/Redis/Qdrant via testcontainers — needs Docker, not `make up`
make test-live         # real Ollama + full stack + Playwright — needs Docker
make up                # docker-compose infra (needs a native Ollama on the host, see README)
make serve             # FastAPI on :8000 (web UI at /)
make reembed COLLECTION_NAME=docs   # rebuild a vector collection under the current embedding identity, swap atomically (ARGS=--dry-run first)
make agent-worker      # queue consumer for the Ecorp domain (also agent-worker-{support,ops,sales})
make chat              # CLI agent; chat-hitl adds approval prompts
```

`make eval`, `promptfoo`, `garak`, `deepeval`, `test-sandbox`, `test-provider-sandbox` need live models or services and
are manual by design. Run `make eval` + `make promptfoo` after a prompt, model-alias or
retrieval change. `make model-eval KIND=embedding|reranker|screen|classifier CANDIDATE=<name>` is the same kind of tier for a
*model swap*: it judges a candidate against the recorded baseline (`evals/README.md`), and `make model-eval-verify`
needs no model or network. Run it before changing an embedding, reranker or screening model, and read the report's
`controls` first. The routing classifier has one more manual target, `make router-label JUDGE=<alias>` (score the local and strong
model's answers, the outcomes any router is judged on); there is no head trainer (`evals/README.md`). A label set (the choices
`POST /v1/decide` picks between) has `make labelset-calibrate CANDIDATE=labelset-<model>`, which chooses its "none of these" cutoff for one
model on a dev split, and is judged with `make model-eval KIND=classifier CANDIDATE=labelset-<model> DATASET=route-topics`.
`make reembed COLLECTION_NAME=<c>` rebuilds a vector collection under the current embedding identity and swaps it in atomically; run
it after changing `EMBED_MODEL`, the prefixes or `EMBED_MODEL_REVISION`. Don't run `make clean`, `clear-*`, or `restart-all` without being asked —
they delete volumes or kill running processes.

## Where things live

- `app/agent/` — the agent, in four packages and one file. `graph/` is the LangGraph: `state.py` is the leaf
  that holds `State`, `STATE_SCHEMA_VERSION` and the budgets; `build.py` is the one builder (`build_graph`,
  with a `GraphProfile` and `GraphDeps`) and the only module that imports every node; `core.py` the turn-entry
  gates; `routing.py` is `should_continue`/`check_output`, whose reasons are the registry in `output_checks.py`;
  `hitl.py` is the approval gate, the rest are one concern each. `runtime/` is the durable singleton and the
  streaming turns (`core.py`, `stream.py`) plus the session directory. `tools/` is `builtin.py` (the tools
  several domains share, with `SHARED_TOOL_CAPABILITIES`), `idempotency.py`, the skill and subagent catalogs
  (one shared `frontmatter.py` loader) and `run_subagent`. `cost/` is the spend meter and caps
  (`COST_GOVERNANCE.md`). `manifest.py` is `AgentManifest`/`DomainPlugin`.
- `app/domains/{ecorp,support,ops,sales}/` — each is `store.py` + `tools.py` + `domain.py`, and
  `registry.py` looks them up by name. Ecorp is the default but has no standing in the graph: `build_graph`
  requires a manifest and a domain. A new use case follows that shape; it never forks `build_graph()`.
- `deploy/` — `compose/` (every `docker-compose*.yml`), `caddy/`, `litellm/` (configs + `patches/`).
  Compose resolves paths and `.env` from the repo root, so run it via the Makefile's `COMPOSE` or
  with `--project-directory .`; a bare `docker compose up` finds no file. `Dockerfile` stays at the
  root (build context), `docker/` holds the auxiliary image builds.
- `app/job_queue/` — Redis Streams queue and workers. `app/ingestion/` — chunking, crawl, upload.
- `app/mcp/` — MCP in both directions (GRAPH_PATTERNS.md 21, 28, 78, 82). The two published servers (`server.py`, `ops_server.py`) are `build_server(identity)` factories: over stdio they act for their **launcher's** identity (`MCP_TENANT`/`MCP_PRINCIPAL`), over HTTP (`gateway.py` + `gateway_auth.py`, the `gateway` compose profile, specs/017) for the verified OAuth 2.1 token of each request, and never for a tool argument; the gateway never reads the launcher's environment, and no write tool or sandbox is behind it (Principle II). `client.py` consumes a remote catalog (pinned by hash, scrubbed then bounded) over `connection.py`'s one long-lived connection per server, which never replays a call that may have run. The app is on `mcp` 2.x (servers on `MCPServer`, client on `ClientSession`); the sandbox bridge, `opensandbox-mcp` 1.1+, is the first release that supports it (`tests/core/test_mcp_sdk_pin.py`).
- `app/retrieval/` — Qdrant helpers, embeddings, the answer cache. `embeddings.py` has two roles (`embed_query`, `embed_document(s)`); `embedding_identity.py` is what a collection's vectors were made with, and `qdrant_store.py` refuses to mix two (pattern 67). `docker/ml-service/ml_service/` is the ML service as an importable package (rerank, prompt guard, `/v1/embeddings`, per-model health and metrics); `docker/ml-service/models.toml` declares the text models it can serve.
- `app/core/` — what every package may use and that depends on none of them: config, security (`SecurityCtx`),
  metrics, errors, scrubbing, resilience, `sql_store.py` (pooled appdata connections), `gateway.py` and
  `model_resolver.py` (the LiteLLM contract), `moderation.py`, and what every tool runs through:
  `tool_runtime.py` (timeout, scrub, fail-closed ctx) and `tool_idempotency.py` (the exactly-once guard). A
  module belongs here when it imports nothing from `agent`, `domains` or `api`.
- `packages/billing/` — the credit wallet, payment webhooks, the billing half of a usage meter (rate, record,
  debit, export), the credit gate and operator CLIs as an independent package (`credit_billing`, src layout,
  own pyproject/tests/schema/alerts/docs/specs). It imports nothing from `app`; the app reaches it only through
  `app/billing_integration/port.py` and carries no billing logic of its own (it hands over six plain fields per
  model call and asks one question per turn); a test on each side enforces it, and one scans the app for any
  billing vocabulary. Edit the package, not the copies: `postgres-init/20-`, `22-`, `24-` and `25-`, the
  package's alert rules in `observability/prometheus/alerts.yml` and its dashboard panels are guarded by
  `tests/core/test_billing_package_copies.py`. Found by path (Makefile `PYTHONPATH`, pytest `pythonpath`);
  not in the lock file. The agent's own meter (`usage_events`, its caps, its gateway reconciliation) stays in `app/agent/`.
- `postgres-init/NN-*.sql` — numbered schema; auto-runs on a fresh volume only.
- `skills/*/SKILL.md`, `subagents/*/AGENT.md` — the agent's own catalogs (not Claude Code's).
- `observability/prometheus/alerts.yml` — alert rules over `app/core/metrics.py` metrics.
- `scripts/model_eval/` + `evals/` — the model-evaluation harness and its frozen data: `evals/{candidates,datasets,baselines,sources}`
  hold what can be evaluated, the versioned sets, the recorded result of the model in use today, and the text the sets are built
  from. Datasets are frozen (a manifest records every file's hash; change one by adding `v2/`), and a baseline is immutable.
  `docker/ml-service/ml_service/` is the ML service as an importable package (on the test/mypy path, not installed).
  `docker/ml-service/labelsets/*.json` are the label sets that service serves (specs/015; the README beside them is the format and the rules).
  `app/retrieval/embedding_identity.py` is what makes mixing two embedding models' vectors impossible (a collection records its model,
  dimension, prefixes and revision, and the app refuses to read or write one that disagrees); `app/core/routing_policy.py` (the rules, the label table and
  `decide`: pure, the evaluation harness runs it too), `app/core/classification.py` (the client for the ML service's label sets) and
  `app/agent/graph/model_route.py` are the router's whole footprint in the app, and it can only write `State.model_route`.
- `promptfoo/` — the prompt checks, and the only Node in the repo: its `package.json`, lock file and `node_modules`
  live here, so every `npm`/`npx` runs from this directory (`make promptfoo` does that). The root stays Python-only.

## Working rules

- Any new `mutating`/`outward` tool needs the full checklist in
  `.claude/rules/side-effect-tools.md` (capability tier, ctx check, `idempotent()`, row-level
  uniqueness, tenant-scoped SQL, tests). Don't add one without it.
- Tune a limit or flag through `app/core/config.py` `Settings` and add it to `.env.example`.
- A broad `except Exception` needs `# noqa: BLE001 - <reason>`; every `# type: ignore[...]` and
  `# noqa` carries a reason. Comments here are long-form on purpose — explain why.
- A bug fix lands with a regression test that fails without it, plus the matching
  `GRAPH_PATTERNS.md` (or the owning package's pattern file) / README update. Disclose a gap you
  leave open rather than omitting it.
- Commits use `fix:`/`feat:`/`test:`/`docs:`/`infra:`/`chore:` prefixes; the body states the
  failure mode and root cause. Work on a topic branch and open a PR to `main`.
- After opening a PR, report its CI result and don't call the work done while checks are pending
  or red. A hook (`.claude/hooks/ci-watch.sh`) watches CI after every `gh pr create` and wakes you
  with the result, labelling each failure "also red on main" (pre-existing) or "NOT red on main"
  (investigate). Its silence is not proof of green — it fails open — so if no report arrives,
  confirm with `gh pr checks <n>` (`test-live` and `deepeval` take 10+ min).
- Keep changes reviewable: one logical change per PR, aiming for ≤ ~400 hand-written lines with
  ~1,000 as the ceiling (generated, lock and scaffold files don't count). If a task will exceed
  that, propose a split before writing code — Spec Kit's task phases are natural PR boundaries —
  and say so when it genuinely can't be split.
- Files: ~500 lines is a prompt to ask whether a module has two responsibilities, not a hard cap.
  It doesn't apply to test files, and this repo's long docstrings are intentional. Split by
  responsibility, never mechanically (no `foo_part2.py`).
- Verify claims about third-party behavior against the installed source or a real run — the repo
  has been bitten by plausible-sounding assumptions (LangGraph resume semantics, redis-py
  timeouts, psycopg transactions).

## Don't touch

- `.env`, `.env.prod`, `CREDENTIALS.local.md` — secrets, gitignored, and blocked for Claude by
  `.claude/settings.json`. Never put their contents in output, logs, commits or docs; edit
  `.env.example` instead.
- `requirements-lock.txt` — machine-generated from `requirements.txt`; regenerate, don't hand-edit.
- `checkpoints.sqlite3*`, `.venv/`, `promptfoo/node_modules/`, `.mypy_cache/` — local artifacts.

## Guardrails

`.claude/settings.json` (shared, committed) enforces what prose can't:
- `deny` on reading or editing `.env`, `.env.prod` and `CREDENTIALS.local.md`.
- `ask` before `make clean`, `make clear-*`, `make obs-clean` and `make restart-all` — they delete
  volumes or kill running processes.
- A `PostToolUse` hook (`.claude/hooks/ruff-check.sh`) runs `ruff check` on each Python file right
  after it is edited and feeds violations back immediately. It is the fast loop for CI's `lint`
  gate, not a replacement for it, and it fails open if ruff isn't installed.
- A second `PostToolUse` hook (`.claude/hooks/ci-watch.sh`, async) waits for CI after every
  `gh pr create` and wakes Claude with a pass/fail summary that separates failures already red on
  `main` from new ones. Set `CLAUDE_CI_WATCH=0` to opt out.

Personal overrides go in `.claude/settings.local.json` (gitignored).

## Spec Kit

This repo uses GitHub Spec Kit: skills in `.claude/skills/speckit-*`, templates and the
constitution in `.specify/`. Flow: `/speckit-specify` → `/speckit-plan` → `/speckit-tasks` →
`/speckit-implement`. A plan's Constitution Check must address each principle it touches, and a
feature that adds a write tool must state its capability tier, tenant scoping and idempotency
story. Amend the constitution only through `/speckit-constitution` and a PR.

Path-scoped guidance loads automatically from `.claude/rules/`: `side-effect-tools.md`,
`runtime-reliability.md`, `testing.md`.

The billing package's own specs (010-013) live in `packages/billing/specs/`; `specs/` keeps a one-file pointer for each, so Spec Kit still counts the numbers as taken (the next is 018; 014 is the model-serving and evaluation-harness spec, 015 builds on it with label sets on the ML service and tiered model routing, 016 puts that behind a decision interface shaped like the hosted Jev one, and 017 is the MCP gateway).
