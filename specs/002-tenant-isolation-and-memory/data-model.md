# Data Model: Tenant Isolation and Cross-Session Memory

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

**Status**: Retrospective — field names, constraints and predicates read from the code and SQL.

## 1. Identity (`SecurityCtx`)

`app/core/security.py::SecurityCtx` — a `TypedDict`, stamped **once** per turn by
`validate_input` from `config["configurable"]["ctx"]` and read-only thereafter.

| Field | Type | Validation | Source |
|-------|------|------------|--------|
| `tenant` | `str` | non-empty (`valid_ctx`: `bool(ctx.get("tenant"))`) | trusted boundary only |
| `principal` | `str` | non-empty | trusted boundary only |
| `claims` | `dict` | none — opaque; **no code branches on a key** | trusted boundary only |

`valid_ctx(ctx)` is a `TypeGuard`: `None` → `False`; missing/empty tenant or principal → `False`.
It is the one check every fail-closed call site makes first, independent of which `Policy` is wired.

**Boundaries that stamp identity** (see [contracts/identity-boundary.md](./contracts/identity-boundary.md)):
HTTP headers · CLI (`local:<os user>`) · Telegram (`telegram:<user_id>`) · seeding (`make-ingest`)
· MCP server (caller-supplied arguments).

**Not identity**: message text, request body fields, tool arguments, model output. A model-visible
tool schema never contains `tenant` or `principal` (the MCP server is the sole exception, by design).

### Policy (access model)

| Method | Contract | Behavior |
|--------|----------|----------|
| `permit(action, ctx) -> bool` | pure; **default-deny** | `False` for an unknown action or any malformed ctx; never raises. Known: `search`, `write_note`, `recall_memory`, `write_memory`, `query_structured_data`. |
| `lower(ctx, target) -> Filter` | store-native predicate | `target ∈ {"documents","memories"}`, else `ValueError`. Applied **inside** the query. |

`ActionAllowlistPolicy(actions)` (domains): `permit` = `action ∈ actions ∧ valid_ctx`; `lower`
raises `NotImplementedError` (their data is relational).

## 2. Scope matrix — what predicate guards what

| Store | Entity | Scope predicate | Enforced at | Hermetic test | Real-backend test |
|-------|--------|-----------------|-------------|---------------|-------------------|
| Qdrant `docs` | Document | `tenant = T ∧ kind = "document"` (+ optional `topic`, `HasId` ANDed) | `Policy.lower` → `_build_filter` → **each** `Prefetch` | `tests/core/test_security.py` | `test_concurrent_turns.py::TestQdrantReadWriteUnderConcurrency` — 6 concurrent tenants, real Qdrant (`integration`) |
| Qdrant `docs` | Memory | `tenant = T ∧ kind = "memory" ∧ owner = P ∧ created_at ≥ now − retention` | same | same | **none** (A4) — the test above seeds `kind="document"` points only |
| Qdrant `docs` | Memory delete | `tenant = T ∧ kind = "memory" ∧ owner = P' ∧ (HasId[id] ⊕ created_at < now − N days)` | `memory.delete_memories` | `tests/agent/test_memory.py` | none |
| Postgres | `employees` | `WHERE tenant = %s` (+ `department =`, `name ILIKE`) | `sql_store.query_employees` | `tests/agent/test_sql_store.py` | none |
| Postgres | tickets, comments, leads, notes, follow-ups | `tenant = %s` on every table and join | `app/domains/{support,sales}/store.py` | `tests/domains/*/test_store.py` (fake cursors) | none |
| Postgres | `chat_sessions` | list: `tenant ∧ principal ∧ domain`; owner check: `thread_id ∧ tenant ∧ principal ∧ domain` | `sessions.list_sessions`, `session_belongs_to` | `tests/agent/test_sessions.py` | none |
| Postgres | `usage_ledger`, `tenant_budget_reservations` | `tenant` | `usage_ledger.py` | `tests/agent/test_usage_ledger.py` | none |
| Redis | Cached answer | `@tenant:{T} ∧ @principal:{P}` (both `_escape_tag`ged) | `semantic_cache.get` | via node tests (mocked) | `test_concurrent_turns.py::TestSemanticCacheIsolationUnderConcurrency` — tenant axis only, real Redis Stack, hyphenated tenant names; **principal axis: none** (feature 001 A3, downgraded) |
| Postgres `checkpointer` | Conversation state | **none — keyed by `thread_id` only** | — | — | `test_concurrent_turns.py::TestNoCrossContaminationUnderConcurrency` proves *concurrent separate threads* do not mix; **ownership across callers: enforced at the API on send, resume and cancel (B2, fixed in #67); not repeated in the worker** |
| Postgres | `tool_call_dedup` | lookup by `tool_call_id` only; `tenant` is metadata | `tool_idempotency._claim_or_cached_result` | `tests/agent/test_tool_idempotency.py` | none (**E2**, feature 003) |
| Postgres | `ops_incidents` | **none — global by design**; attributed by `opened_by` | `app/domains/ops/store.py` | `tests/domains/ops/` | — (**E1**) |

## 3. Persisted entities

### 3.1 Document point (Qdrant `docs`)

Written by ingestion and by the approved `add_note` tool.

| Payload field | Type | Written by | Notes |
|---------------|------|-----------|-------|
| `text` | str | both | the embedded chunk |
| `kind` | `"document"` | both | part of every document filter |
| `tenant` | str | both | stamped from ctx |
| `topic` | str (`langgraph`\|`qdrant`\|`company`) | `add_note`; ingestion optional | closed `Topic` enum on `add_note` |
| `title` | str ≤ 200 | both | |
| `source`, `ingested_by`, `parent_id`, `parent_text` | str | ingestion only | `ingested_by = ctx["principal"]`; `add_note` records **no writer** |

Point id: ingestion → `uuid5(ns, f"{tenant}|{source}|{index}|{sha256(text)}").hex` (**tenant is part
of the id**); `add_note` → `uuid5(ns2, tool_call_id).hex`. Vectors: named `dense` (cosine) and
`sparse` (BM25, IDF).

### 3.2 Memory point (Qdrant `docs`)

Written **only** by `_remember_impl`.

| Payload field | Type | Rule |
|---------------|------|------|
| `text` | str | non-blank, **≤ 2 000 chars** (`RememberArgs`) |
| `kind` | `"memory"` | part of every memory filter |
| `tenant` | str | `ctx["tenant"]` — never caller-supplied |
| `owner` | str | `ctx["principal"]` — never caller-supplied |
| `created_at` | ISO-8601 UTC | `datetime.now(UTC).isoformat()` at write; read by retention and by `older_than_days` |

Point id `uuid5(ns, tool_call_id).hex` ⇒ a replay of the same call upserts onto the same point.

### 3.3 `chat_sessions` (Postgres `appdata`)

| Column | Type | Constraint | Notes |
|--------|------|-----------|-------|
| `thread_id` | TEXT | PRIMARY KEY | **client-supplied** (UUID4 by default; Telegram `telegram:<chat_id>`) |
| `tenant` | TEXT | NOT NULL | |
| `principal` | TEXT | NOT NULL | |
| `title` | TEXT | NOT NULL | ≤ 60 chars + `…`; set on first insert only |
| `domain` | TEXT | NOT NULL DEFAULT `'ecorp'` | set on first insert only; immutable |
| `created_at`, `last_active_at` | TIMESTAMPTZ | default `now()` | upsert refreshes only `last_active_at` |

Index `(tenant, principal, domain, last_active_at DESC)`. Upsert is
`ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()` — so a second caller on an existing
`thread_id` **does not change the owner** (the row stays the first caller's). It could not by itself stop B2 because it was never consulted on the send, resume and cancel paths; since #67 `claim_session` (an insert that does nothing on conflict, then a scoped read) is.

### 3.4 Cached answer (Redis Stack) — see feature 001 data-model §3.3

Scope fields `tenant`, `principal` are TAG-indexed; values are escaped for RediSearch syntax.

### 3.5 Deletion audit record

Not a stored row. Two outputs per `delete_memories` call: counter
`agent_memory_deletion_total{outcome ∈ {deleted, refused}}` (**no tenant/principal labels**) and a
structured log line — `memory_deleted` with `{tenant, principal, selector ∈ {memory_id,
older_than_days}, count}` on success, or `memory deletion refused: ambiguous selector` with
`{tenant, has_memory_id, has_older_than_days}`. A missing/malformed ctx raises `ValueError` and
counts `refused`.

## 4. Memory lifecycle

```text
 (none) ──remember(tool call)──► proposed ──approve──► stored ──┬─ age < retention ──► recalled (visible)
                                    │                            │
                                    ├─ reject ─────► (none)      ├─ age ≥ retention ──► expired (stored, INVISIBLE)
                                    ├─ cancel ─────► (none)      │
                                    └─ unattended ─► (none)      └─ delete_memories ──► deleted (gone)
                                       (auto-decline)
```

- *proposed → stored* requires the mandatory approval gate (feature 003); an unattended caller
  auto-declines, so a memory is never written by a fire-and-forget path.
- *stored* is visible only to `(tenant, owner)` and only while `created_at ≥ now − retention`.
- A memory with **no** `created_at` (written before the field existed) is **expired from birth** —
  invisible, not "permanent" (per the code's comment; behavior not verified by a real-store test).
- *expired → gone* happens only if an operator runs `delete_memories`; nothing does automatically
  and nothing currently calls it (A3).

## 5. Conversation ownership

```text
 unseen thread_id ──first turn start──► claimed (chat_sessions row: tenant, principal, domain) ──► owned
                                                                                                    │
   GET messages / GET pending_approval ── session_belongs_to(ctx, thread_id, domain) ──► 200 | 404 ┘
   POST send (claims if new) / POST resume / POST cancel ── _require_conversation_owner ─► 202 | 404   (#67)
   worker job ────────────────────────────────────────────────────────────────────────► NO CHECK  (relies on the API)
```

State that is keyed by `thread_id` alone (and therefore not isolated by caller): the checkpoint
(`checkpointer` DB), the per-thread Redis lock `agent:lock:<thread_id>`, and the cancel flag
`agent:cancel:<thread_id>`.

## 6. Validation rules (traceable to FRs)

| Rule | FR |
|------|----|
| `tenant`, `principal` non-empty or refuse; no default | FR-003 |
| Unknown policy action ⇒ deny; malformed ctx ⇒ deny, never raise | FR-004 |
| Narrowing filters AND onto scope, never replace it | FR-008 |
| `query_employees`: `department ∈ {Engineering, Support, Sales}`; row cap 20 + truncation marker | FR-009 |
| `remember`: non-blank, `len ≤ 2000` | FR-019 |
| Retention: exclude `created_at < now − MEMORY_RETENTION_DAYS` (365) and missing `created_at` | FR-022 |
| `delete_memories`: exactly one of `memory_id`/`older_than_days`; valid ctx; tenant-scoped | FR-023 |
| Deletion audit: outcome counter without tenant/principal labels + structured log | FR-024 |
| Ingestion without valid ctx ⇒ `IngestRefused`, counted by reason | FR-015 |
| Ingest point id includes tenant | FR-016 |
