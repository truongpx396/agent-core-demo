# Implementation Plan: Tenant Isolation and Cross-Session Memory

**Branch**: `002-tenant-isolation-and-memory` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-tenant-isolation-and-memory/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

Isolation is enforced as a **store-native predicate inside every query**, keyed on one immutable
`SecurityCtx` stamped at the trusted boundary. A tiny pure `Policy` answers two questions: *may
this action happen* (`permit`, default-deny) and *what predicate expresses what this caller may
see* (`lower`, producing a Qdrant `Filter`). Documents scope to `tenant`; memories scope to
`tenant` **and** `owner` **and** a retention window; relational stores always add `WHERE tenant =
%s`; the semantic cache scopes to `tenant` **and** `principal`; the session directory scopes to
`tenant`, `principal`, `domain`. Cross-session memory is write-opt-in (the gated `remember` tool),
read-automatic (folded into the pre-fetch), and deletable only by an operator-called function with
an audit trail.

The one structural hole this plan records honestly: the conversation **checkpoint** is keyed by
`thread_id` alone and the ownership check that compensates for that exists only on two read
endpoints (**B2**, Principle I deviation).

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `qdrant-client==1.19.0` (`Filter`, `FieldCondition`, `MatchValue`,
`DatetimeRange`, `HasIdCondition`, `FilterSelector`), `psycopg[binary,pool]==3.3.5`,
`redis==8.1.0` + RediSearch (via Redis Stack) for the semantic cache, `fastapi==0.141.1`
(required `Header(...)` dependencies), `mcp==1.29.0` (`FastMCP`, pinned — 2.x removed
`mcp.server.fastmcp`), `pydantic` v2 (tool `args_schema`), `fastembed==0.8.0`.

**Storage**: Qdrant collection `docs` — one collection for documents *and* memories, discriminated
by payload `kind`, payload-filtered (no per-tenant collection, **no payload indexes created**);
Postgres `appdata` (`employees`, `support_tickets`, `support_ticket_comments`, `crm_leads`,
`crm_lead_notes`, `crm_followups`, `chat_sessions`, `usage_ledger`; `ops_incidents` deliberately
global); Postgres `checkpointer` (conversation state, no tenant column); Redis Stack
(`cache:*` JSON docs, `idx:semantic_cache` with TAG fields `tenant`, `principal`).

**Testing**: pytest hermetic tier for filter construction, SQL text/params and fake-store
behavior (`tests/core/test_security.py`, `tests/agent/test_memory.py`,
`tests/agent/test_sessions.py`, `tests/agent/test_sql_store.py`, `tests/mcp/test_mcp_server.py`,
`tests/ingestion/test_ingestor.py`, `tests/domains/*/test_store.py`). Real-backend
isolation tests exist only for document search across six concurrent tenants (real Qdrant) and the answer cache's
tenant axis (real Redis Stack), both in `tests/agent/test_concurrent_turns.py` (`integration` tier); memory
owner/retention, the relational stores, the session directory and the cache's principal axis have none (A4).

**Target Platform**: Linux containers; trusted-header deployment behind a gateway that is *not*
shipped (see A5).

**Project Type**: Web service + workers + CLI + chat-bot + MCP server (identity enters through
five different boundaries — see data-model.md §1).

**Performance Goals**: None asserted. Retention and tenant filters are evaluated by Qdrant on
unindexed payload fields; correctness is unaffected, throughput at large corpus size is unmeasured.

**Constraints**: Policy contract is "pure — no I/O, no clock, no randomness"; `MEMORY_RETENTION_DAYS`
default 365; memory content ≤ 2 000 chars; employee-query cap 20 rows; tool-call timeout 15 s.

**Scale/Scope**: Two seeded tenants (`ecorp`, `other-co`) exist purely so isolation tests have
something real to prove against; Telegram users all share the default tenant.

**Unknowns**: none — every value is read from the repository.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | **Primary** | **PASS with 1 deviation (B2) and 2 deliberate exceptions** | **PASS parts**: ctx only from `config["configurable"]["ctx"]` (`app/agent/graph.py::validate_input`); scoping inside the store query — Qdrant `Policy.lower` applied in **each** `Prefetch` (`app/retrieval/qdrant_store.py::_build_filter`, `hybrid_search`), SQL `WHERE tenant = %s` (`app/agent/sql_store.py::query_employees`, `app/domains/*/store.py`), cache TAG filter (`app/retrieval/semantic_cache.py`); every tool re-checks ctx (`app/agent/tools.py::_ctx_or_refuse`, `app/mcp/server.py`); child tables carry `tenant` (`postgres-init/15-…`, `08-crm.sql`); missing ctx ⇒ refusal (`route_after_validation` → `reject_context`). **Deviation B2**: the checkpoint read on the send/resume/cancel paths is not scoped to the caller — see Complexity Tracking. **Exception E1**: `ops_incidents` is deliberately global (`postgres-init/10-ops-incidents.sql` header). **Exception E2**: `tool_call_dedup` is looked up by call id alone (`app/agent/tool_idempotency.py::_claim_or_cached_result`; owner: feature 003). |
| II | Mandatory approval (NN) | Yes — memory write | **PASS** (owner: 003) | `remember` and `add_note` are `mutating` in `TOOL_CAPABILITIES` (`app/agent/tools.py`), so `should_continue` routes them through `human_approval` unconditionally. |
| III | Fixed, typed tools | Yes | **PASS** | `args_schema` on `remember` (`max_length=2000`, blank rejected), `add_note`, `query_employees` (closed `Department` enum, no `execute(sql)`); write identity derived by code (`_tool_call_point_id` uuid5, `_content_point_id`); no agent-facing delete (`app/agent/memory.py` module docstring). |
| IV | Exactly-once side effects (NN) | Yes — memory write | **PASS** (owner: 003) | `remember` wraps `idempotent(...)` and uses a `tool_call_id`-derived point id, so a replay upserts onto the same point. |
| V | Bounded, observable failure | Yes | **PASS with 1 advisory** | Recall degrades to empty (`gather_context`, counted via `agent_context_retrieval_degraded_total`); deletion is counted `agent_memory_deletion_total{outcome}` with **no tenant/principal labels** and logged; rate limiting keyed by tenant (`app/api/rate_limit.py`). **Advisory A6**: nothing alerts on a stuck/zero-success deletion path — moot today because nothing calls it (A3). |
| VI | Untrusted content is data | Yes — memory | **PASS** | Memories are numbered into the same `<retrieved_document>`-framed context as documents (`app/agent/tools.py::gather_context`); written only by the gated tool, re-filtered by tenant+owner on every recall; no code extracts memories from turn text. |
| VII | Test discipline | Yes | **PASS with 1 advisory (A4)** | Hermetic coverage of every filter/SQL shape (listed above). **A4 (partial)**: real-backend isolation proof exists for documents (6 concurrent tenants, real Qdrant) and the cache's tenant axis (real Redis Stack, hyphenated tenant names) — `tests/agent/test_concurrent_turns.py`, `integration` tier — but **not** for memories (owner, retention), the relational stores, the session directory or the cache's principal axis; and two third-party behaviors the design leans on are asserted only in comments: *Qdrant treats a missing field as never matching a range filter* (memory excluded when `created_at` absent) and *`HasIdCondition` ANDs with the tenant filter*. Principle VII says such claims MUST be verified against a real run. |
| VIII | Why-first docs, honest gaps | Yes | **PASS with new disclosures** | Patterns 17, 18, 19, 21, 22, 33 carry their motivating bugs. B2, the missing-auth proxy, the unreachable deletion function and the ungoverned ops domain were **not** previously disclosed in `GRAPH_PATTERNS.md`/README (the README Roadmap does disclose "real authentication"); they are disclosed here. |

**Gate result (pre-research)**: one unjustified violation (B2) → **not waivable** under
Governance ("A violation that cannot be avoided MUST be recorded and justified in Complexity
Tracking, not merged silently"). B2 *can* be avoided, so it is recorded as an **open defect**, not
justified. The plan proceeds because this is a retrospective description of shipped code, not a
merge request; the constitution's gate would block a PR that introduced B2.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged on B2; the
contracts make the unchecked paths explicit (see `contracts/conversation-ownership.md` endpoint
matrix), which is how E1/E2 and the Telegram id derivation were noticed. No new design-level
violation.

## Project Structure

### Documentation (this feature)

```text
specs/002-tenant-isolation-and-memory/
├── plan.md
├── spec.md
├── research.md                       # Phase 0 — decisions, each with its motivating bug
├── data-model.md                     # Phase 1 — identity, scopes per store, memory lifecycle
├── quickstart.md                     # Phase 1 — runnable isolation / memory / deletion checks
├── contracts/
│   ├── identity-boundary.md          # how identity enters; headers; fail-closed shapes
│   ├── scoping-matrix.md             # obligations any new store/tool must meet + the missing real-backend recipe
│   ├── conversation-ownership.md     # endpoint-by-endpoint ownership check matrix (B2)
│   └── memory-operations.md          # remember (tool), recall (automatic), delete (operator)
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── core/
│   ├── security.py          # SecurityCtx, Policy Protocol, TenantIsolationPolicy, DEFAULT_POLICY, valid_ctx
│   └── config.py            # MEMORY_RETENTION_DAYS, DEFAULT_TENANT
├── agent/
│   ├── tools.py             # _ctx_or_refuse, search_docs/_document_hits/_memory_hits, gather_context,
│   │                        #   remember/_remember_impl, add_note, query_employees, _tool_call_point_id
│   ├── memory.py            # delete_memories (operator-only; one selector; audited)
│   ├── sql_store.py         # query_employees — fixed SQL, mandatory tenant predicate
│   ├── sessions.py          # upsert_session / list_sessions / session_belongs_to
│   ├── runtime_stream.py    # get_session_messages / get_pending_approval (no ctx by design)
│   ├── graph.py             # validate_input stamps ctx once; route_after_validation fails closed
│   └── usage_ledger.py      # tenant-scoped usage (cost governance owns the rest)
├── retrieval/
│   ├── qdrant_store.py      # _build_filter, hybrid_search (filter in each Prefetch), delete/count_by_filter
│   └── semantic_cache.py    # tenant+principal TAG filter, _escape_tag
├── ingestion/ingestor.py    # valid_ctx gate, payload tenant + ingested_by, _content_point_id(tenant,…)
├── domains/policy.py        # ActionAllowlistPolicy (lower() raises: no Qdrant data)
├── mcp/server.py            # tenant/principal as explicit args + DEFAULT_POLICY.permit
├── channels/{chat,telegram}.py   # local / Telegram ctx stamping
└── api/main.py              # get_ctx (required headers), get_domain, session & usage endpoints

postgres-init/
├── 02-appdata.sql           # employees (+ seeded 'ecorp' and 'other-co')
├── 06-chat-sessions.sql / 11-chat-sessions-domain.sql
├── 07-support-tickets.sql / 08-crm.sql / 15-append-notes-as-rows.sql   # tenant on every table + child tables
└── 10-ops-incidents.sql     # deliberately NOT tenant-scoped

scripts/seed.py              # fixed 'make-ingest' identity under DEFAULT_TENANT

tests/
├── core/test_security.py, agent/test_memory.py, agent/test_sessions.py, agent/test_sql_store.py,
│   mcp/test_mcp_server.py, ingestion/test_ingestor.py, domains/*/test_store.py, api/test_api.py
└── (partly missing) integration/test_isolation_real_backends.py  → task T022 (A4); documents + cache tenant axis already in agent/test_concurrent_turns.py
```

**Structure Decision**: No new package: isolation is a *discipline applied inside existing
modules*, which is why this feature has no single directory. The seam is `app/core/security.py`
(policy) plus the store layer; each store's module docstring states its own predicate. Conversation
ownership lives in `app/agent/sessions.py` and is invoked from the HTTP layer, not from the graph —
the structural reason B2 exists.

## Complexity Tracking

> Filled because the Constitution Check found one deviation and two deliberate exceptions.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B2 (defect, open)** — conversation state is keyed by `thread_id` alone; `session_belongs_to` guards only `GET …/messages` and `GET …/pending_approval`. Send, resume and cancel never check, nor does the worker. Reproduced at graph level; Telegram ids are `telegram:<chat_id>`. | Not needed — an accident of putting the ownership check in the HTTP read handlers because the checkpointer "has no tenant/principal to check against" (`get_session_messages` docstring). | Fix options in `research.md` R15. The structural one (namespace the checkpoint key by tenant+principal so a client *cannot address* another's thread) removes the need for a per-endpoint check but changes cancel-flag, lock and session keys and Telegram's id. A per-endpoint check is smaller but must also cover first-use claiming of a new id. Either is its own PR (tasks.md). |
| **E1** — `ops_incidents` is not tenant-scoped; the ops domain is reachable by any caller who names it in `X-Domain`. | Documented design: incidents concern the platform's own metrics, which have no tenant dimension (SQL header, `app/domains/ops/store.py` docstring). | A tenant column would be meaningless. What *is* missing is authorizing which tenants may use the domain at all (per-action/per-domain authorization, README Roadmap). Not a constitution wording the code satisfies; flagged so the constitution can either carve out the exception or the gap can be closed. |
| **E2** — `tool_call_dedup` lookup is `WHERE tool_call_id = %s` with no tenant predicate. | Provider-assigned call ids are assumed globally unique; the table's `tenant` column is observability metadata only. | Adding `AND tenant = %s` is one line, but changes what a cross-tenant id collision means (a miss ⇒ a second real write). Owned and tracked by feature 003. |
| **A3** — the deletion function has no caller (no script, endpoint, make target). | Pattern 33 deliberately kept deletion out of the agent; the "real data-subject-request script" it anticipates was never written. | A thin `scripts/` entry point (like `scripts/tool_call_dedup_sweep.py`) is small; left as a task, not built in a docs batch. |
| **A4 (partial)** — real-backend isolation proof covers documents and the cache's tenant axis only; memories (owner, retention), relational stores, sessions and the cache's principal axis are proven only by hermetic tests; two third-party behaviors are asserted only in comments. | Hermetic tiers were the priority; the existing real-backend tests were written for concurrency, not isolation per se; the constitution already records the fake-cursor gap for SQL stores. | `tests/containers.py` already provides real Qdrant/Postgres/Redis Stack and the existing suite shows the pattern; extending it to the missing stores is cheap and is what turns Principle I from "believed" to "proven" for them. |
| **A5** — shipped production proxy neither authenticates nor sets/strips identity headers. | The repo ships the app's trust seam, not the gateway; README Roadmap lists real authentication as unbuilt. | Adding header-stripping in `Caddyfile` is cheap (`request_header -X-Tenant-Id` …) but meaningless without an authenticator that sets them; the honest minimum is a prominent deployment warning (task). |
| **A6** — no alert on the deletion path. | Not applicable while A3 stands. | Revisit when A3 is closed. |
