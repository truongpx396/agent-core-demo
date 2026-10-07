# CLAUDE.md

A production-shaped, fully local LangGraph RAG agent: LiteLLM proxy → Ollama, Qdrant, Postgres,
Redis Streams, Langfuse, FastAPI. Three example domains (support, ops, sales) run on one
unmodified graph. Python 3.13.

Read these before changing behavior; they hold the reasoning this file deliberately omits:
- `.specify/memory/constitution.md` — the non-negotiable rules. Principles I, II and IV
  (tenant isolation, mandatory approval, exactly-once side effects) are NON-NEGOTIABLE.
- `GRAPH_PATTERNS.md` — 54 numbered patterns, each with the real bug that motivated it.
  "Extending Further" is the honest list of what is and isn't built.
- `README.md` — architecture, make targets, testing tiers. `WORKER_CONCURRENCY.md` — worker sizing.

## Commands

The Makefile assumes an activated venv: `source .venv/bin/activate`.

```bash
make lint            # ruff check .          (CI gate)
make typecheck       # mypy over app/ scripts/ (CI gate)
make test            # pytest -n auto -q — hermetic, no services needed (CI gate)
pytest tests/agent/test_tool_idempotency.py::test_name -q   # one test; marked tiers are skipped by default
make test-integration  # real Postgres/Redis/Qdrant via testcontainers — needs Docker, not `make up`
make test-live         # real Ollama + full stack + Playwright — needs Docker
make up                # docker-compose infra (needs a native Ollama on the host, see README)
make serve             # FastAPI on :8000 (web UI at /)
make agent-worker      # queue consumer for the Ecorp domain (also agent-worker-{support,ops,sales})
make chat              # CLI agent; chat-hitl adds approval prompts
```

`make eval`, `promptfoo`, `garak`, `deepeval`, `test-sandbox` need live models or services and
are manual by design. Run `make eval` + `make promptfoo` after a prompt, model-alias or
retrieval change. Don't run `make clean`, `clear-*`, or `restart-all` without being asked —
they delete volumes or kill running processes.

## Where things live

- `app/agent/` — the graph. `graph.py` (State, nodes, `STATE_SCHEMA_VERSION`), `graph_build.py`
  (`build_graph`), `graph_routing.py` (`should_continue`), `graph_hitl.py` (approval gate),
  `runtime*.py` (durable singleton + streaming turns), `tools.py` (`TOOL_CAPABILITIES`),
  `tool_idempotency.py`, `sql_store.py` (pooled appdata connections), `manifest.py`
  (`AgentManifest`/`DomainPlugin`).
- `app/domains/{support,ops,sales}/` — each is `store.py` + `tools.py` + `domain.py`. A new use
  case follows that shape; it never forks `build_graph()`.
- `deploy/` — `compose/` (every `docker-compose*.yml`), `caddy/`, `litellm/` (configs + `patches/`).
  Compose resolves paths and `.env` from the repo root, so run it via the Makefile's `COMPOSE` or
  with `--project-directory .`; a bare `docker compose up` finds no file. `Dockerfile` stays at the
  root (build context), `docker/` holds the auxiliary image builds.
- `app/job_queue/` — Redis Streams queue and workers. `app/ingestion/` — chunking, crawl, upload.
- `app/core/` — config, security (`SecurityCtx`), metrics, errors, scrubbing, resilience.
- `postgres-init/NN-*.sql` — numbered schema; auto-runs on a fresh volume only.
- `skills/*/SKILL.md`, `subagents/*/AGENT.md` — the agent's own catalogs (not Claude Code's).
- `observability/prometheus/alerts.yml` — alert rules over `app/core/metrics.py` metrics.

## Working rules

- Any new `mutating`/`outward` tool needs the full checklist in
  `.claude/rules/side-effect-tools.md` (capability tier, ctx check, `idempotent()`, row-level
  uniqueness, tenant-scoped SQL, tests). Don't add one without it.
- Tune a limit or flag through `app/core/config.py` `Settings` and add it to `.env.example`.
- A broad `except Exception` needs `# noqa: BLE001 - <reason>`; every `# type: ignore[...]` and
  `# noqa` carries a reason. Comments here are long-form on purpose — explain why.
- A bug fix lands with a regression test that fails without it, plus the matching
  `GRAPH_PATTERNS.md` / README update. Disclose a gap you leave open rather than omitting it.
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
- `checkpoints.sqlite3*`, `.venv/`, `node_modules/`, `.mypy_cache/` — local artifacts.

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
