# Spec 019: ports and adapters

**Status**: built, in one PR at the owner's request (a commit series, one port family per commit). The design record is [`ARCHITECTURE.md`](../../ARCHITECTURE.md);
this file states what was required and how it was checked. No `plan.md` or `tasks.md` was generated: the work was one refactor with a measured worklist (below), not a feature.

## Why

A review of the import graph found the project layered, not hexagonal: 4 `Protocol`s in 24k lines; the cost policy, the graph, the shared tools and the domain tools importing `psycopg`,
`httpx`, `openai`, `langchain_openai`, `qdrant_client` and the adapter modules directly; the security kernel carrying a Qdrant type; the runtime importing the concrete domain registry; and
no test that would have caught any of it. One AST pass measured the worklist: 40 violations in 26 files.

## Requirements

- **FR-1** The application (`app/agent`, `app/domains` minus the adapter files) imports no infrastructure client and no adapter. The kernel (`app/core` minus the platform modules) and the ports import nothing above them.
- **FR-2** Every external system the application uses is reached through a `Protocol` in `app/ports/`, bound at one composition root (`app/bootstrap.py`). An unbound port fails closed.
- **FR-3** Every process that runs the application binds the ports before it does anything else, and a test fails the build if one does not.
- **FR-4** Behaviour is unchanged: the moved SQL and HTTP are verbatim, every fail-open and fail-closed choice and every degrade counter stays where it was.
- **FR-5** A fake and an adapter are held to the same Protocol by a test, so a test double cannot drift.
- **FR-6** The dependency rule is enforced as a ratchet: a ledger of the violations that existed, which could only shrink, and ended empty.

## Constitution check

| Principle | How it holds |
|---|---|
| **I. Tenant isolation** | `Policy.lower` returns a store-neutral `AccessScope`; `DocumentIndex.search` cannot be called without one; the unscoped skills lookup is a separate, named `search_catalog`. The Qdrant filter is built in one function (`scope_filter`) pinned condition by condition and against the real policy. Every repository method still takes the tenant. The real-Qdrant isolation proof is in the integration tier (not run here: see below). |
| **II. Mandatory approval** | Untouched: no tool, tier or gate moved. The tools keep `idempotent()`, `ctx_or_refuse` and their capability declarations. |
| **IV. Exactly-once** | `ToolCallLedger` keeps the atomic `INSERT ... ON CONFLICT DO NOTHING RETURNING` claim, moved verbatim; the row-level `tool_call_id UNIQUE` writes are the domain stores' and untouched; Ecorp's deterministic point id stays in the use case (`tool_call_point_id`). |
| **V. Bounded, observable failure** | Every degrade path keeps its counter: the policy stayed in `app/agent/cost/` and the kernel, and only the statements moved. |
| **VI. Untrusted content** | Framing, scrubbing and the SSRF guard stayed with the tools and the transports; a store driver's error text still never reaches a prompt (`register_store_error`). |
| **VII. Test discipline** | The default suite is still hermetic. The SQL-shape tests were retargeted at the adapters, the isolation tests now assert the scope, and the scanners have tests that make them fail. |

## Success criteria and how they were checked

| Criterion | Result |
|---|---|
| The ledger of layering violations | 40 entries at the start, empty at the end (`tests/core/test_architecture_layers.py::LEDGER`) |
| Hermetic suite | 4,888 passed and 1 skipped (the baseline on `main` was 4,786, so ~100 tests were added); the one failure on the baseline was `tests/scripts/test_usage_reconcile.py`'s sigterm test, which passes alone and failed on `main` under load before this change, and did not fail in the last full runs |
| `make lint`, `make typecheck` | clean (ruff; mypy over 285 files) |
| An entrypoint that forgets `wire()` | fails `tests/core/test_entrypoints_wire.py` (mutation-checked on `scripts/seed.py`) |
| A forbidden import | fails `tests/core/test_architecture_layers.py` (mutation-checked with `import psycopg` in `app/agent/cost/spend.py`) |
| The integration, live and e2e tiers | **not run** (Docker and models); every test in them still collects, and their Postgres fixtures were rewritten to bind the real adapters (`tests/integration/postgres_adapters.py`) |

## Not done

See "Not done, and why" in `ARCHITECTURE.md`: the inbound adapters and the ingest pipeline still reach some infrastructure directly (they are adapters by the rule); the graph's own nodes are bound to LangChain
messages; the constitution is not amended (a MINOR addition, through `/speckit-constitution`).
