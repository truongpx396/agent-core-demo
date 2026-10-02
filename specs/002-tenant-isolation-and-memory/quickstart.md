# Quickstart: Validate Tenant Isolation and Cross-Session Memory

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier
first. **Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves the *shape* of every scoping predicate and refusal. It
> does **not** prove isolation against a real store, except where the integration tier already does:
> document search across concurrent tenants (real Qdrant) and the answer cache's tenant axis (real Redis
> Stack) — everything else is unproven against a real service (plan A4). *Scenario B2* below shows the one isolation hole that Tier 1 cannot see.

---

## Tier 1 — Hermetic (no services, ~4 s)

```bash
pytest tests/core/test_security.py tests/agent/test_memory.py tests/agent/test_sessions.py \
       tests/agent/test_session_messages.py tests/agent/test_sql_store.py tests/mcp/test_mcp_server.py \
       tests/ingestion/test_ingestor.py tests/agent/test_tools.py tests/api/test_api.py tests/domains -q
```

**Expected** (observed 2026-10-02): `371 passed`.

| Requirement | Evidence |
|-------------|----------|
| FR-003/FR-004 `valid_ctx`, default-deny `permit`, never raises | `test_security.py::TestValidCtx`, `TestTenantIsolationPolicyPermit` |
| FR-007 document vs memory filters (tenant, kind, owner, retention window) | `test_security.py::TestTenantIsolationPolicyLower` |
| FR-009 `query_employees` always tenant-scoped; filters AND, never replace | `test_sql_store.py` |
| FR-006 MCP tool refuses without identity; two tenants ⇒ two different `tenant` params | `test_mcp_server.py` |
| FR-012 session list scope; owner check; invalid ctx ⇒ empty / false without querying | `test_sessions.py` |
| FR-014–FR-016 ingestion refuses without ctx; two tenants never collide on a point id | `test_ingestor.py` |
| FR-019/FR-018 `remember`: writes owner-stamped memory; registered as a tool | `test_tools.py` |
| FR-023/FR-024 deletion: one selector, tenant-scoped, counted, audited | `test_memory.py` |

## Scenario B2 — Reproduce the conversation-ownership gap (hermetic; expected: it reproduces)

Establishes that a conversation's state is partitioned by `thread_id` alone. No services. In a
scratch Python session (do not commit):

1. `build_graph(GraphDeps(llm=<GenericFakeChatModel with two plain answers>, search_docs=<async
   no-op returning ("", [])>, cache_get=<async no-op returning None>, cache_set=<async no-op>))`.
2. Run a turn with `config["configurable"] = {"thread_id": "T", "ctx": {tenant: "tenant-a",
   principal: "alice", claims: {}}}` and message `"Our internal codename is PINEAPPLE-7"`.
3. Run a second turn with the **same** `thread_id` but ctx `{tenant: "tenant-b", principal:
   "mallory", …}` and message `"what did we discuss earlier?"`.
4. Read `(await g.aget_state({"configurable": {"thread_id": "T"}})).values`.

**Observed 2026-10-02**: both human messages are in the thread's history (tenant A's included in the
context tenant B's turn ran on) and `state["ctx"]` is now tenant B's. **Fixed when**: the second turn
cannot address tenant A's thread (it either runs on a fresh, B-owned history or is refused). This
scenario is the failing test the fix PR starts with.

## Tier 2 — Real Postgres / Redis Stack / Qdrant (Docker, no model)

```bash
make test-integration
```

**Expected**: passes or self-skips if Docker is unreachable. Relevant here:
`tests/agent/test_concurrent_turns.py` — `TestQdrantReadWriteUnderConcurrency` (6 tenants, each surfaces only its
own document, real Qdrant) and `TestSemanticCacheIsolationUnderConcurrency` (a second tenant's first ask never hits
another's cached entry, real Redis Stack). These prove **SC-001 for documents and the cache's tenant axis only**.
Memories by owner/retention, the relational stores, the session directory and the cache's principal axis have no
real-backend test; closing plan A4 means extending that suite per
[contracts/scoping-matrix.md](./contracts/scoping-matrix.md). Do not cite this tier as evidence for them.

## Tier 3 — Full local stack, manual walk-through

**Prerequisites**: as feature 001's quickstart Tier 3 (`make up`, `make pull-models`, `make ingest`,
`make serve`, `make agent-worker` in a second terminal). Define a helper (a function, not a
variable of flags — unquoted variables are not word-split in zsh):

```bash
chat() { curl -N -X POST localhost:8000/chat/stream/queued \
  -H 'Content-Type: application/json' -H "X-Tenant-Id: $1" -H "X-Principal-Id: $2" -d "$3"; }
# usage: chat ecorp alice '{"message":"What are Ecorp support hours?","thread_id":"iso-1"}'
```

Seeding put sample documents under tenant `ecorp`; the SQL seed also created a second tenant
`other-co` with one employee, purely so isolation has something real to prove against.

| # | Request | Expected | Proves |
|---|---------|----------|--------|
| 1 | `chat ecorp alice '{"message":"What are Ecorp support hours?","thread_id":"iso-1"}'` | grounded answer with `citations` | baseline (US1) |
| 2 | same question as `chat other-co bob '{"message":"What are Ecorp support hours?","thread_id":"iso-2"}'` | **no** Ecorp citation (other-co has no documents) — a general-knowledge or "no documents" style answer | SC-001, FR-006/007 |
| 3 | `chat ecorp alice '{"message":"Who works in Support?","thread_id":"iso-3"}'` then repeat as `other-co`/`bob` | ecorp rows only; then the single `other-co` employee only | FR-009 |
| 4 | omit `X-Principal-Id` (or `X-Tenant-Id`) | HTTP **422**, no stream | FR-003, [identity-boundary.md](./contracts/identity-boundary.md) |
| 5 | `curl -H 'X-Tenant-Id: ecorp' -H 'X-Principal-Id: alice' localhost:8000/chat/sessions` then with `X-Principal-Id: carol` | alice sees `iso-1`, `iso-3`; carol sees none | FR-012, US3 |
| 6 | `curl -i … -H 'X-Principal-Id: carol' localhost:8000/chat/sessions/iso-1/messages` | **404** `{"detail":"session not found"}` — and the same 404 for an id that never existed | SC-008 |
| 7 | **Remember**: as alice ask "Please remember that I prefer short answers." | the turn ends with `approval_required` for `remember`; nothing stored yet. *Model-dependent*: the small local model may answer without calling the tool — if so, rephrase ("Use your remember tool to save: I prefer short answers.") | FR-018, US4 |
| 8 | approve it: `curl -N -X POST localhost:8000/chat/resume -H … -d '{"thread_id":"<id>","approved":true}'` | stream finishes with "Remembered."-style reply | FR-018/FR-019 |
| 9 | **Recall**: new `thread_id`, same alice: "How do I like my answers?" | the memory appears as a cited source `[n]` | US4 scenario 4 |
| 10 | same question as `carol` (same tenant) | memory **not** recalled | SC-003 |
| 11 | never approve (reject at step 7) then ask in a new thread | nothing recalled — nothing stored | SC-006 |

### Delete a memory (operator action — there is no entry point; plan A3)

No script, endpoint or make target exists. To exercise it by hand against the **local** stack (it
reads `QDRANT_URL` from your environment — check it points at the local Qdrant), from the repo root
with `PYTHONPATH=.`. **This permanently deletes every memory alice has in tenant `ecorp`.**

```python
import asyncio
from app.agent.memory import delete_memories
ctx = {"tenant": "ecorp", "principal": "alice", "claims": {}}
print(asyncio.run(delete_memories(ctx, older_than_days=0)))   # selector: ONE of memory_id / older_than_days
```

**Expected**: prints the number removed (alice's memories only); calling with both selectors or
neither raises `ValueError`; the `agent_memory_deletion_total{outcome=…}` counter and a
`memory_deleted` log line record each call. Afterwards, recall (step 9) returns nothing.

## Verifying deployment-level isolation (A5)

Inspect the deployed edge: send a request **directly** to the public address with arbitrary
`X-Tenant-Id` / `X-Principal-Id`. If it is answered, the deployment has no authenticating gateway and
isolation protects against bugs only (research R17). This is a read-only check; do not use real
tenant names.

## Troubleshooting

- *Step 2 returns an Ecorp citation*: stop — that would be a real isolation failure. Capture the
  request headers, the `citations` event and the Qdrant filter in use, and treat it as a security
  incident before continuing.
- *Step 9 recalls nothing after approving*: check the worker logs for a refused/timed-out
  `remember`, and that approval was sent with the same `thread_id` and an identity header pair.
- *Do not* run `make clean`, `clear-*` or `restart-all` while validating.
