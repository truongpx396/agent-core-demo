# CLAUDE.md

Graph Agent Core: a production-shaped, multi-tenant, multi-domain LangGraph RAG agent. LiteLLM
proxy → any OpenAI-compatible LLM (Ollama in dev), Qdrant, Postgres, Redis Streams, Langfuse,
FastAPI. Four example domains (Ecorp, support, ops, sales) run on one unmodified graph. Python 3.13.

Read these before changing behavior; they hold the reasoning this file deliberately omits:
- `ARCHITECTURE.md` — the hexagonal layout: rings, the ports and their adapters, the dependency rule and the tests that enforce it, how to add a port, and what is not done.
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

The Makefile assumes an activated venv: `source .venv/bin/activate`. It holds the stack, tests and lint and includes `make/host.mk` (the app's processes on the host),
`make/admin.mk` (back-office commands), `make/evals.mk`, `make/security.mk` and `make/test.mk` (tests, lint, load tests, smoke); target names are the same wherever they live. The env-example test scans `.mk` too.

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
make agent-worker      # queue consumer for the Ecorp domain (AGENT_WORKER_DOMAINS=support,ops picks others; agent-worker-all serves every domain)
make chat              # CLI agent; chat-hitl adds approval prompts (turns go through the queue: run `make agent-worker` too)
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
  (`COST_GOVERNANCE.md`). `AgentManifest`/`DomainPlugin` (the plugin contract) are in `app/ports/outbound/domains.py`. Outside `app/agent`, a domain imports only `app.agent.tools` and a driving adapter only `app.agent.runtime` and `app.agent.cost` (`tests/core/test_architecture_layers.py`).
- `app/domains/{ecorp,support,ops,sales}/` — each is a **module** (`ARCHITECTURE.md`, "The business modules"): `domain.py` + `tools.py` + `store.py` + `ports.py` (its repository port), and
  `registry.py` looks them up by name. A module imports no other module (`app/domains/shared/` holds `policy.py`, `sandbox_session.py` and `web_search.py`, which they share), and code outside
  `app/domains/` imports only its `domain` and `usecases` (`tests/core/test_architecture_layers.py`). Ecorp is the default but has no standing in the graph: `build_graph`
  requires a manifest and a domain. A new use case follows that shape; it never forks `build_graph()`. A domain's skills and subagents are
  its `skills/<name>/SKILL.md` and `subagents/<name>/AGENT.md` (the agent's own catalogs, not Claude Code's): the folder is the owner, there is no
  `domains:` tag, and `app/domains/shared/skills/` would hold a skill every domain can follow (none today: each skill needs a tool only some domains have, and `tests/domains/test_skills_name_only_tools_their_domain_has.py` checks it); a subagent always belongs to one domain.
- `deploy/` — what runs the processes: `compose/` (every `docker-compose*.yml`: the stack, `.collectors` the exporters/OTel collector/Promtail overlay, `.observability` the backends), `caddy/`, `litellm/` (configs + `patches/`),
  `images/` (the sandbox image builds), `terraform/` (the droplets; `deploy/README.md` is the runbook), `env/`.
  Compose resolves paths and `.env` from the repo root, so run it via the Makefile's `COMPOSE` or
  with `--project-directory .`; a bare `docker compose up` finds no file. The app's `Dockerfile` stays at the root (build context).
- `services/` — what runs as its own process and owns its Dockerfile, dependencies and README: `authlimit/` (below) and `ml-service/` (rerank, prompt guard, `/v1/embeddings`, label sets: the `ml_service/` package, `models.toml`, `labelsets/`; built with its own directory as the context, on the test/mypy path but not installed). `packages/` is the other kind: a library the app imports.
- `app/adapters/inbound/` — the DRIVING side, where something outside calls the application (`ARCHITECTURE.md`); leaves, nothing imports them. `api/` is FastAPI (`routers/`, `deps.py`, `identity.py`, `health.py`, the web UI in `static/`). `channels/` are the CLI (`cli/`) and the Telegram bot (`telegram/`), clients of the chat API through the front door (`door.py`, with the sign-in in `oauth.py`); `tests/adapters/inbound/channels/test_channels_use_the_door.py` holds that they never reach the queue or the graph (GRAPH_PATTERNS.md 89, 91; specs/020). The bot's own parts, in `telegram/`, are `bot.py` (the flow), `approvals.py` (the buttons), `inbox.py` (where updates wait and the per-chat dispatcher), `server.py` (its webhook and link pages) and `link.py` (account linking): GRAPH_PATTERNS.md 92 to 94, specs/021. `workers/` are the queue consumers (`agent_worker.py`, `ingest_worker.py`) and own their Redis Streams code (`streams.py`, `ingest_streams.py`); the API is the only producer. `mcp/` publishes MCP (GRAPH_PATTERNS.md 21, 28, 78, 82): the two servers (`server.py`, `ops_server.py`) are `build_server(identity)` factories that over stdio act for their **launcher's** identity (`MCP_TENANT`/`MCP_PRINCIPAL`), over HTTP (`gateway.py` + `gateway_auth.py`, the `gateway` compose profile, specs/017) for the verified OAuth 2.1 token of each request, and never for a tool argument; the gateway never reads the launcher's environment, and no write tool or sandbox is behind it (Principle II). `app/ingestion/` is the ingest pipeline (chunking, extractors, the ingestor) the upload route and the ingest worker run: application code, over the `Embedder`, `DocumentWriter` and `PageFetcher` ports.
- `services/authlimit/` — the edge authentication and rate-limit service (specs/018), **the only Go in the repo**: a self-contained module (own tests, Dockerfile, CI job) that Caddy asks about every request before the chat API and the MCP routes. It verifies the Casdoor token offline, charges per-tenant and per-person token buckets in Redis, refuses revoked people, and mints a 30-second signed identity. It imports nothing from `app/` and `app/` imports nothing from it (`tests/core/test_authlimit_boundary.py`); they share only the contract, `testdata/auth-vectors/` and two JSON registries. The API's side of the seam is `app/platform/internal_identity.py` (the verifier) and `app/adapters/inbound/api/identity.py`: `AUTH_MODE=identity` (the default) trusts only the identity the door signs and ignores `X-Tenant-Id`/`X-Principal-Id`; the hermetic tests and `.env.example` choose `headers` explicitly. `make authlimit-test`; `make authlimit-smoke` (needs Docker; `ARGS="--upstream api"` after `make authlimit-up-app` runs it against the real API); `make authlimit-web-check` (the web page in a real browser). Constitution 1.4.0, "Non-Python services"; GRAPH_PATTERNS.md 84, 85, 86; ENGINEERING_PATTERNS.md 86, 88.
- `app/adapters/outbound/` — the DRIVEN side, one package per technology, each implementing a port: `postgres/`, `litellm/`, `mlservice/` (the client for the ML service), `web/`, `notify/`, `minio/`, `crawl4ai/`, `qdrant/` (the vector store and memory deletion), `redis/` (the answer cache and the producer side of the Streams queues) and `embeddings/` (`embeddings.py` has two roles, `embed_query` and `embed_document(s)`; `embedding_identity.py` is what a collection's vectors were made with, and `qdrant_store.py` refuses to mix two: pattern 67). `mcp/` is the client side: `client.py` consumes a remote catalog (pinned by hash, scrubbed then bounded) over `connection.py`'s one long-lived connection per server, which never replays a call that may have run, and `sandbox_tools.py` is the sandbox bridge. The app is on `mcp` 2.x (servers on `MCPServer`, client on `ClientSession`); the bridge, `opensandbox-mcp` 1.1+, is the first release that supports it. Never imports `inbound/`.
- `app/ports/`, `app/adapters/`, `app/bootstrap.py` — **ports and adapters** (`ARCHITECTURE.md`). `app/ports/outbound/` holds the
  Protocols the application needs (cost, models, llm, sessions, idempotency, checkpointing, retrieval, classification,
  screening, web, notify, sandbox, jobs, storage, domains; a domain's own repository port is `app/domains/<d>/ports.py`), each with a `Slot` the application reads at call
  time (`ticket_store.get().get_ticket(...)`; unbound raises `PortNotBound`). `app/adapters/outbound/<technology>/`
  implements them; a domain's `store.py` is the one adapter that stays in its domain.
  `app/bootstrap.py::adapters()` is the one table of port to adapter; every entrypoint calls `wire()` first. In `app/agent` and
  `app/domains` import no infrastructure client (psycopg, redis, qdrant_client, openai, httpx, ...) and no adapter:
  `tests/core/test_architecture_layers.py` fails the build. Tests bind a fake with `Slot.overriding` (`tests/fakes/`).
- `app/core/` — what every package may use and that depends on none of them: config, security (`SecurityCtx`, `AccessScope`),
  metrics, errors, scrubbing, resilience, `gateway.py` (the identity the gateway is told) and `moderation.py`, and what every
  tool runs through: `tool_runtime.py` (timeout, scrub, fail-closed ctx) and `tool_idempotency.py` (the exactly-once guard). A
  module belongs here when it imports nothing from `agent`, `domains`, `api` or an adapter, and no infrastructure client. The
  The process plumbing is not here but in `app/platform/` (`sql_store.py`, `redis_client.py`, `internal_identity.py`, `telemetry.py`, `profiling.py`, `logging_config.py`, `job_runtime.py`): it opens a connection or a socket, so it is adapter-side (`ARCHITECTURE.md`), and the application may not import it.
  The containers reach the app's two databases through PgBouncer in transaction mode (specs/022, `WORKER_CONCURRENCY.md`): every pool passes
  `POOLER_SAFE_CONNECTION_KWARGS` (no prepared statements), and no SQL may keep state on a session (`tests/core/test_pooler_safe_sql.py`).
  `profiling.py` pushes CPU profiles to Pyroscope when `PYROSCOPE_SERVER_ADDRESS` is set (off otherwise, never raises).
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
- `scripts/` — run as `python -m scripts.<group>.<name>`: `jobs/` (scheduled one-shots), `checks/` (against a live stack or model: smokes, probes, `eval`), `tools/` (operator and developer CLIs); `model_eval/` and `ai_review/` are packages.
- `observability/prometheus/alerts.yml` — alert rules over `app/core/metrics.py` metrics.
- `scripts/model_eval/` + `evals/` — the model-evaluation harness and its frozen data: `evals/{candidates,datasets,baselines,sources}`
  hold what can be evaluated, the versioned sets, the recorded result of the model in use today, and the text the sets are built
  from. Datasets are frozen (a manifest records every file's hash; change one by adding `v2/`), and a baseline is immutable.
  `services/ml-service/labelsets/*.json` are the label sets that service serves (specs/015; the README beside them is the format and the rules).
  `app/adapters/outbound/embeddings/embedding_identity.py` is what makes mixing two embedding models' vectors impossible (a collection records its model,
  dimension, prefixes and revision, and the app refuses to read or write one that disagrees); `app/core/routing_policy.py` (the rules, the label table and
  `decide`: pure, the evaluation harness runs it too), `app/adapters/outbound/mlservice/classification.py` (the client for the ML service's label sets) and
  `app/agent/graph/model_route.py` are the router's whole footprint in the app, and it can only write `State.model_route`.
- `promptfoo/` — the prompt checks, and the only Node in the repo: its `package.json`, lock file and `node_modules`
  live here, so every `npm`/`npx` runs from this directory (`make promptfoo` does that). The root stays Python-only.

## Working rules

- Any new `mutating`/`outward` tool needs the full checklist in
  `.claude/rules/side-effect-tools.md` (capability tier, ctx check, `idempotent()`, row-level
  uniqueness, tenant-scoped SQL, tests). Don't add one without it.
- Tune a limit or flag through `app/core/config.py` `Settings` and add it to `.env.example`.
- A new external system (a store, a service, a provider) is a port in `app/ports/outbound/`, an adapter, and one entry in `app/bootstrap.py::adapters()`; never an import of its client in `app/agent` or `app/domains`. A new entrypoint calls `wire()`. Both are tests, not conventions (`ARCHITECTURE.md`, "Adding something").
- A broad `except Exception` needs `# noqa: BLE001 - <reason>`; every `# type: ignore[...]` and
  `# noqa` carries a reason. Comments explain *why* (the failure that motivated the code), not
  what; keep them short, point at the bug or pattern number, and leave the long reasoning to the
  pattern docs.
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
  It doesn't apply to test files. Split by responsibility, never mechanically (no `foo_part2.py`).
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

The billing package's own specs (010-013) live in `packages/billing/specs/`; `specs/` keeps a one-file pointer for each, so Spec Kit still counts the numbers as taken (014 is the model-serving and evaluation-harness spec, 015 builds on it with label sets on the ML service and tiered model routing, 016 puts that behind a decision interface shaped like the hosted Jev one, 017 is the MCP gateway, 018 is the edge authentication and rate-limit service, 019 is the hexagonal restructuring: ports, adapters and a composition root, 020 puts the CLI and the Telegram channel behind the front door, 021 gives the Telegram channel approvals by button, parallel chats, a webhook and account linking, and 022 puts PgBouncer in front of the app's databases and adds Pyroscope profiling; the next is 023).
