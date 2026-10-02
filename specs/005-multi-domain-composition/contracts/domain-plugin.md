# Contract: The Domain Seam (manifest, plugin, registry) and the New-Domain Recipe

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §1–§2](../data-model.md) | **Routing**: feature 004 [worker-process.md](../../004-scalable-serving-and-front-doors/contracts/worker-process.md)

**Status**: Retrospective — `app/agent/manifest.py`, `app/agent/graph_build.py`, `app/domains/registry.py`,
`app/domains/policy.py`; exercised by `tests/agent/test_manifest.py` and `tests/domains/test_registry.py`.

## The two halves of a domain

| Half | Type | Holds | Rule |
|------|------|-------|------|
| Configuration | `AgentManifest` (frozen) | `name`, `system_prompt`, `allowed_tools` | The prompt is a **ctx-free constant** (no tenant, principal, timestamp). `allowed_tools` = the plugin's `tools()` names. |
| Plugin | `DomainPlugin` (Protocol) | `tools()`, `tool_capabilities()`, `policy()` | `tool_capabilities()` maps **every** tool to `read_only | mutating | outward`; a missing name is treated as `outward` (feature 003). `policy()` is informational — `build_graph()` never calls it; each tool enforces its own policy. |

## What `build_graph(manifest, domain)` does with them

- Builds the **same** node and edge topology for every domain; no branch on a domain name exists in the graph.
- Binds the domain's capability map into `should_continue` once per call (`functools.partial`).
- Gives the tool executor only the plugin's tools; any other requested name is bounced to the model (feature 003).
- Uses `manifest.system_prompt` as the system message and as the reference text for the prompt-leak check.
- Stashes the manifest on the compiled graph (`compiled.manifest`); `runtime.py::_ensure_seeded_async` reads it to seed a new thread with the
  domain's prompt.
- With no arguments it uses `DEFAULT_MANIFEST` / `DEFAULT_DOMAIN_PLUGIN` (the original assistant, unchanged).

## Registry

`DOMAINS: dict[str, tuple[AgentManifest, DomainPlugin]]` — `"ecorp"`, `"support"`, `"ops"`, `"sales"`.
`resolve_domain(name)` returns the pair or raises `ValueError("Unknown AGENT_DOMAIN '<name>' — must be one of: …")`.

| Entry point | Reads the name from | On an unknown name |
|-------------|--------------------|--------------------|
| worker process, chat-app process | `AGENT_DOMAIN`, once at start | raises; the process exits |
| HTTP chat endpoints | the `X-Domain` header (default `ecorp`) | **422** `Unknown X-Domain '<name>' — must be one of: …` |
| terminal, `ops_investigate.py` | not read — the terminal serves the default domain; the script imports `OPS_MANIFEST` directly | n/a |

One process serves one domain for its life. A thread stays on the domain that opened it (feature 002's ownership gate keys on it).

## `ActionAllowlistPolicy`

`ActionAllowlistPolicy(frozenset[str])`: `permit(action, ctx)` is true iff `action` is in the set **and** `valid_ctx(ctx)`; `lower()` raises
`NotImplementedError` (these domains hold no Qdrant-scoped data). Each tool calls `_ctx_or_refuse(config, "<tool name>")` →
`valid_ctx` **and** `POLICY.permit` before anything else; failing either returns the fixed refusal text and does nothing.

## Recipe — adding a domain

1. `app/domains/<name>/store.py` — fixed, parameterized SQL; a `tenant = %s` predicate on every statement (or a documented exception); child
   tables carry their own `tenant`; inserts and appends keyed by `tool_call_id`.
2. `app/domains/<name>/tools.py` — a Pydantic `args_schema` per tool (length bounds on free-form fields — **A7**; enums for categorical ones);
   write tools follow the feature-003 checklist (`ctx check → idempotent() → timed impl`); an `ActionAllowlistPolicy` naming every tool.
3. `app/domains/<name>/domain.py` — the prompt constant, `skill_tools_first(...)` ordering, `make_skill_tools("<name>")`, an optional
   `make_domain_subagent_tool(...)`, the plugin and the manifest.
4. A numbered `postgres-init/NN-*.sql` that continues the sequence (and a header saying how an existing volume applies it).
5. A `DOMAINS` entry in `app/domains/registry.py`.
6. A worker service (`docker-compose.yml`, `docker-compose.prod.yml`) with `AGENT_DOMAIN`, and run targets in the `Makefile`.
7. Tests: `tests/domains/test_registry.py` entry; `tests/domains/<name>/test_domain.py` (sandbox set, the default-only tools **absent**, scoped
   delegation, approval gate per write tool); `test_store.py` (statement shape); and a `SAMPLE_ARGS` entry per write tool in
   `tests/domains/test_write_tools_contract.py` (the test fails with a pointer to the checklist if one is missing).
8. Docs: the README *Example domains* table and a `GRAPH_PATTERNS.md` entry if the domain taught a pattern; **correct any docstring that names
   the tool set** (A1).

## Invariants a change must preserve

1. No branch on a domain name in the graph; a new domain never forks `build_graph()`.
2. A domain's tool list is its sandbox: an omission is a restriction, never an accident.
3. Every write tool of every domain passes the contract test (identity refusal; `idempotent()` with the injected id and its own name).
4. A system prompt stays a ctx-free constant.
5. An unknown domain name never starts a process or serves a request.
