# Research: Tenant Isolation and Cross-Session Memory

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-02

**Status**: Retrospective — decisions reconstructed from the code, its comments and
`GRAPH_PATTERNS.md` patterns 17, 18, 19, 21, 22, 33, 49. Each entry names its evidence. **No
`NEEDS CLARIFICATION` remains.** Entries R15–R18 are *findings* from verifying the as-built
system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**.

---

## R1. A store-native pre-filter, applied inside every retrieval leg

- **Decision**: `Policy.lower(ctx, target)` returns a Qdrant `Filter` that is passed *into* the
  query — and into **each** `Prefetch` of a hybrid (dense + sparse) search, not only the fused
  top level. A Python post-filter over an unscoped result is forbidden.
- **Rationale**: A buggy post-filter and a correct one look identical until a cross-tenant leak
  appears on an untested query; a store-native predicate fails loudly instead. Filtering only after
  RRF fusion would let the fused candidate set briefly include rows the caller cannot see. Narrowing
  inputs (`topic`, `doc_ids`, department) are ANDed onto the tenant predicate, so they can only
  intersect, never widen.
- **Alternatives considered**: post-filter in Python (rejected above); a separate collection per
  tenant (strongest physical separation, but one collection per tenant to provision, migrate and
  index — and the code's own comment is explicit that the shared collection "ISN'T a correctness
  boundary Qdrant itself enforces", so the id space and every query must keep tenants apart
  themselves).
- **Evidence**: `app/core/security.py::TenantIsolationPolicy.lower`;
  `app/retrieval/qdrant_store.py::_build_filter`, `hybrid_search`; pattern 17;
  `tests/core/test_security.py::TestTenantIsolationPolicyLower`.

## R2. Two nested axes in one collection, discriminated by `kind`

- **Decision**: Documents and memories share the `docs` collection. `target="documents"` scopes to
  `tenant` + `kind=document`; `target="memories"` scopes to `tenant` + `kind=memory` + `owner` +
  `created_at ≥ now − retention`. `kind` is itself part of the filter.
- **Rationale**: A memory belongs to whoever wrote it — a second isolation axis inside the first.
  Putting `kind` in the predicate stops a document query from returning a memory point and vice
  versa without a second collection.
- **Evidence**: `app/core/security.py`; `app/agent/tools.py::_document_hits`, `_memory_hits`.

## R3. A pure policy with default-deny, and the contract wording that is not quite true

- **Decision**: `Policy.permit(action, ctx)` is default-deny: unknown actions and malformed ctx
  return `False` (never raise). Known actions: `search`, `write_note`, `recall_memory`,
  `write_memory`, `query_structured_data`. Domains use `ActionAllowlistPolicy` (same fail-closed
  `permit`; `lower()` raises `NotImplementedError` because their data is relational).
- **Rationale**: A policy that can crash on bad input hands the caller an exception to mishandle
  into failing open; a policy that calls out to a database could itself become the outage that
  allows or denies everything.
- **Finding (minor)**: the Protocol says "PURE — no I/O, no clock, no randomness", but
  `TenantIsolationPolicy.lower` reads the clock (`datetime.now(UTC)`) to compute the retention
  cutoff. Isolation is unaffected; the wording is inaccurate and makes the function harder to test
  deterministically (tests must tolerate a moving cutoff or patch time).
- **Evidence**: `app/core/security.py`, `app/domains/policy.py`.

## R4. Fail closed twice: at turn entry and again inside every tool

- **Decision**: `route_after_validation` checks `valid_ctx` *before* the empty-input check and
  routes to `reject_context` (counted); every ctx-aware tool calls `_ctx_or_refuse` itself and
  returns a refusal `ToolMessage` (never raises).
- **Rationale**: A tool call is a different code path from the entry node, so "an upstream node
  already checked" is not a guarantee. Putting the infra-fault case ahead of the user-error case
  stops a missing identity from reading as "you typed nothing".
- **Evidence**: `app/agent/graph.py`, `app/agent/tools.py::_ctx_or_refuse`; tests
  `tests/agent/test_routing.py::TestRouteAfterValidationCtx`.

## R5. Identity stamped once, from trusted headers that are a seam — not authentication

- **Decision**: `get_ctx` declares `X-Tenant-Id` and `X-Principal-Id` as required `Header(...)`
  dependencies, so a request missing either is a 422 before the handler runs; no body field can set
  identity and there is no default. `claims` is carried but opaque. `X-Domain`, by contrast,
  *defaults* (to `ecorp`) because absence is the normal pre-existing case, but an *unknown* value
  is a 422 so a typo cannot publish onto a stream no worker reads.
- **Rationale**: Fail-closed in the *shape* of the dependency rather than a runtime `if`. Real
  authentication later should be a gateway configuration change, not a rewrite.
- **Alternatives considered**: verifying a JWT in the app (out of scope; the code states the seam
  is meant to be filled by a gateway).
- **Consequence recorded**: the repo ships the seam, not the gateway — see R17/A5.
- **Evidence**: `app/api/main.py` module docstring, `get_ctx`, `get_domain`; README Roadmap
  ("Real authentication").

## R6. Fixed typed queries; an external protocol server takes explicit identity

- **Decision**: Structured data is reachable only through `query_employees` — closed `Department`
  enum, optional `name_contains`, parameterized SQL with a mandatory `tenant = %s`, `LIMIT cap+1`
  to detect truncation, a visible `[truncated: …]` marker. The MCP server has no `RunnableConfig`
  channel, so `tenant`/`principal` are explicit tool arguments checked against the same
  `DEFAULT_POLICY.permit`, then passed to the same tenant-predicated query.
- **Rationale**: The access boundary lives in code a reviewer can read, not in a string the model
  writes. For MCP, "what is demonstrated is the query-layer scoping"; authenticating the *caller*
  is deliberately left to a production deployment.
- **Evidence**: `app/agent/sql_store.py`, `app/agent/tools.py::query_employees`, `app/mcp/server.py`
  module docstring; patterns 15, 21; `tests/mcp/test_mcp_server.py`.

## R7. Child tables carry their own tenant, and reads use it

- **Decision**: Every relational table that holds tenant data has a `tenant` column, including
  child tables (`support_ticket_comments`, `crm_lead_notes`, `crm_followups`), and a join to a child
  repeats the predicate (`LEFT JOIN … ON c.ticket_id = t.id AND c.tenant = t.tenant`). Row lookups
  by id still include the tenant (`WHERE t.tenant = %s AND t.id = %s`), so another tenant's id
  returns `None`, never the row.
- **Rationale**: A child that inherits tenant only through its parent breaks silently the moment a
  query joins it carelessly.
- **Evidence**: `postgres-init/08-crm.sql`, `15-append-notes-as-rows.sql`;
  `app/domains/support/store.py::get_ticket`; constitution Principle I.

## R8. Content-addressed ids include the tenant

- **Decision**: Ingestion derives each point id as `uuid5(ns, "tenant|source|index|sha256(text)")`;
  tool-written points derive from `tool_call_id`.
- **Rationale**: Because the collection is payload-filtered, two tenants uploading byte-identical
  content must not collide onto one point (the second would silently overwrite the first's
  `tenant` payload). Folding tenant into the id keeps the id space apart on its own. Re-ingesting
  one tenant's identical content *intentionally* upserts onto the same ids (idempotent retry).
- **Evidence**: `app/ingestion/ingestor.py::_content_point_id`;
  `tests/ingestion/test_ingestor.py::…different_tenants_never_collide_onto_the_same_point_id`.

## R9. Memory: writing is opt-in, reading is not

- **Decision**: The only way a memory is created is the `remember` tool (`mutating` ⇒ mandatory
  approval ⇒ `idempotent()` ⇒ uuid5 point id from `tool_call_id`). Recall is automatic, folded
  into `gather_context` alongside documents, re-filtered on every call, never cached, and framed as
  retrieved data. There is no model-invokable read or delete tool.
- **Rationale**: Whatever writes memory decides what is replayed into every future prompt; an
  autonomous write would be a privileged side channel. "A poisoned document affects one answer; a
  poisoned memory replays on every later turn until removed." Re-filtering every call means a
  clearance change takes effect on the very next turn.
- **Alternatives considered**: automatic fact extraction from turns (rejected: exactly the side
  channel above); a model-facing "forget" tool (rejected: a harder trust question than gating the
  writes).
- **Evidence**: `app/agent/tools.py::remember`, `gather_context`, `recall_memories` (no production
  caller — enrichment goes through `gather_context`); pattern 18.

## R10. Retention enforced at recall time; no sweep exists

- **Decision**: `Policy.lower` for memories ANDs `created_at ≥ now − MEMORY_RETENTION_DAYS`
  (default 365) onto every recall; `created_at` is stamped by `_remember_impl` (UTC ISO 8601), never
  caller-supplied.
- **Rationale**: An unswept expired memory is invisible at read time regardless of whether a
  background job ever ran. A memory with no `created_at` is excluded as well, per the code comment
  "Qdrant treats a missing field as never matching a range filter" — **a third-party behavior
  asserted in a comment and not verified by any test** (plan A4; Principle VII).
- **Consequence**: expired points are never physically removed unless an operator runs deletion
  (R11), and nothing does.
- **Evidence**: `app/core/security.py`, `app/core/config.py` (`memory_retention_days`), pattern 33.

## R11. Deletion: operator-only, exactly one selector, audited — and unreachable

- **Decision**: `delete_memories(ctx, *, memory_id | older_than_days, target_principal=None)`
  requires **exactly one** selector (both or neither raises `ValueError` and counts `refused`),
  always scopes to `ctx["tenant"]`, defaults to the caller's own principal, may target another
  principal *within the tenant*, counts matches then deletes with the same `Filter`, and records
  `agent_memory_deletion_total{outcome=deleted|refused}` (no tenant/principal labels — cardinality
  and privacy) plus a structured log line carrying tenant, principal, selector, count.
- **Rationale**: An ambiguous selector must not silently narrow to "everything". The count-then-
  delete race is accepted at demo scope (Qdrant's delete returns no count). The function does not
  authenticate that the caller is *entitled* to target another principal.
- **Finding (A3)**: nothing in `app/` or `scripts/` calls it, and no Makefile target exists; only
  tests do. The "real data-subject-request script" pattern 33 anticipates was never written.
- **Evidence**: `app/agent/memory.py`, `app/retrieval/qdrant_store.py::delete_by_filter/
  count_by_filter`; `tests/agent/test_memory.py`.

## R12. The answer cache is at least as narrow as the memory filter

- **Decision**: Redis Stack KNN restricted to `tenant` **and** `principal` TAG fields, with both
  values passed through `_escape_tag`; any failure is a miss.
- **Rationale**: A cached answer can carry citations into a principal's own memories. Real bug:
  RediSearch TAG queries treat `-` (and other punctuation) as syntax, so an unescaped tenant such as
  `other-co` raised a syntax error — fixed by `_escape_tag`, which the real-Redis tenant test covers only
  *incidentally* (hyphenated tenant names; feature 001 advisory A3, downgraded). The cache key also ignores conversation context (feature 001 gap G3), a
  wrong-context risk within one principal.
- **Evidence**: `app/retrieval/semantic_cache.py`; pattern 22.

## R13. A session directory exists because the checkpoint has no owner

- **Decision**: `chat_sessions(thread_id PK, tenant, principal, title, domain, …)` is written
  best-effort at the *start* of every turn, listed by `(tenant, principal, domain)`, and consulted
  by `session_belongs_to` before a transcript or pending-approval read. Both reads answer an
  unowned id with **404**, never 403, so existence is not disclosed. `domain` was added (pattern 49)
  because resuming under a different domain would run that domain's tools and prompt against
  history never built around them; it is set on first insert and immutable.
- **Rationale**: The checkpointer's own tables carry no tenant/principal, and parsing owner out of
  its blob format would be fragile across version bumps. `get_session_messages`/
  `get_pending_approval` therefore take no ctx and delegate ownership to the *caller* — which is
  precisely the seam B2 falls through (R15).
- **Evidence**: `app/agent/sessions.py`, `app/agent/runtime_stream.py`, `app/api/main.py`;
  `tests/agent/test_sessions.py`, `tests/agent/test_session_messages.py`.

## R14. Unguessable server-generated ids are a *documented* boundary — for some ids

- **Decision**: Chat results streams (`request_id`) and ingest job streams (`job_id`) are not
  ownership-checked; both are server-generated `uuid4().hex` ("unguessable in practice").
- **Rationale**: Those ids are minted by the server per request, so the posture is coherent for
  them. It does **not** extend to `thread_id`, which the *client* chooses.
- **Evidence**: `app/api/main.py::ingest_stream` docstring; `queue.py::results_stream_key`.

## R15. FINDING B2 — the conversation checkpoint is keyed by `thread_id` alone

- **What was found**: `session_belongs_to` is called from exactly two places in `app/`: the
  transcript and pending-approval `GET`s. `POST /chat/stream/queued`, `/chat/resume`, `/chat/cancel`
  and the worker's job handlers never call it. The graph's state is addressed by
  `config["configurable"]["thread_id"]`; the checkpoint carries no owner.
- **Evidence level**: *Graph level, reproduced* — two different tenants and principals ran turns on
  one `thread_id` against one in-memory checkpointer; the second turn ran on the first tenant's
  history and `state.ctx` was overwritten with the second tenant's identity (hermetic script, fake
  model, no services). *Endpoint level, by reading* — the call-site grep above. *Not reproduced*
  against a running API + worker + Postgres.
- **Predictable ids**: the Telegram channel derives `telegram:<chat_id>` (`_thread_id_for_chat`), a
  small integer; the default web `thread_id` is a UUID4 but any client string is accepted.
- **Impact (by reading)**: (1) *Confidentiality* — a caller who supplies another's `thread_id` has
  that history placed in the model's context and can ask about it. (2) *Integrity/availability* —
  the same caller can approve, reject or cancel another's pending action; because
  `astream_events_resume` re-supplies ctx, an approved write runs under the **resumer's** identity,
  not the owner's. The cancel flag is also keyed by `thread_id` alone.
- **Candidate fixes (none built)**:
  1. **Namespace the checkpoint key** at the API boundary — e.g. `thread_id =
     hash(tenant, principal, domain, client_thread_id)`. A client then *cannot address* another's
     checkpoint; no per-endpoint check is needed. Cost: cancel-flag, lock and session keys and
     Telegram's id derivation change; existing checkpoints are orphaned (migration or a read-through
     for legacy ids).
  2. **Check ownership on every thread-addressed endpoint** with *claim-on-first-use* semantics
     (atomic `INSERT … ON CONFLICT DO NOTHING` into `chat_sessions` before publishing, then compare
     owner). Smaller, but must handle the race between claim and first turn and must not make a
     brand-new id look "foreign".
  3. **Defense in depth in the worker** (`process_request`): verify ownership before running any
     job kind, so a non-API producer cannot bypass the API check.
  Recommendation: (1) as the structural fix, (3) as the net under it; write the failing
  two-tenant test first (see `tasks.md`).

## R16. How identity enters, per channel

| Boundary | Tenant | Principal | Notes |
|----------|--------|-----------|-------|
| HTTP | `X-Tenant-Id` (required) | `X-Principal-Id` (required) | trusted-layer headers; the demo UI makes both editable |
| CLI (`make chat`) | `DEFAULT_TENANT` (`ecorp`) | `local:<OS user>` | so the CLI sees what `make ingest` seeded |
| Telegram | `DEFAULT_TENANT` | `telegram:<user_id>` | each user is its own principal; thread `telegram:<chat_id>` |
| Seeding (`make ingest`) | `DEFAULT_TENANT` | `make-ingest` | unowned content is never ingested as tenant-less |
| MCP server | caller argument | caller argument | same `permit` gate; caller not authenticated |

`DEFAULT_TENANT` is a *stamped* identity for local boundaries, not a fallback for a missing
identity — the fail-closed rule applies to the HTTP path.

## R17. FINDING — deployment: the shipped proxy provides no authentication

- **What was found**: `Caddyfile` does TLS, load balancing and health checks and then
  `reverse_proxy`s to `api`. It neither authenticates nor sets/strips `X-Tenant-Id` /
  `X-Principal-Id`; `docker-compose.prod.yml` exposes only Caddy. The `api` module docstring says
  the service "is safe only behind a gateway that authenticates the caller and sets these headers
  itself, stripping client-supplied copies". README Roadmap lists real authentication as unbuilt.
- **Consequence**: deployed as shipped, any internet caller can name any tenant and principal;
  isolation protects against bugs, not against header choice.
- **Evidence level**: read from the two files; not tested against a deployed instance.

## R18. FINDING — deliberate non-isolation and unindexed filters

- **`ops_incidents` (E1)** has no `tenant` column by design (platform metrics have no tenant
  dimension); the ops domain's tools prove only "a legitimate caller of this deployment".
  `get_domain` validates that a domain *exists*, not that the tenant may use it.
- **No Qdrant payload indexes** are created anywhere (`create_payload_index` is never called), so
  tenant/kind/owner/`created_at` filters run on unindexed fields. Correctness is unaffected; the
  performance at large corpus size is unmeasured, and Qdrant's own multi-tenancy guidance would be
  worth checking before a large deployment.
- **Evidence**: `postgres-init/10-ops-incidents.sql` header; `app/api/main.py::get_domain`;
  grep for `create_payload_index`.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B2 | Close the conversation-ownership gap (R15) with the failing two-tenant test first | Structural decision (namespace vs. per-endpoint check) and a data migration; its own PR |
| A3 | A runnable operator entry point for memory deletion | Needs a decision on how an operator authenticates the data-subject request |
| A4 | Extend real-backend isolation proof to what is *not* covered today (memories by owner and retention, `HasId` ∧ tenant, the relational stores, the session directory, the cache's principal axis); verify the two comment-only Qdrant behaviors. Documents and the cache's tenant axis are already proven in `tests/agent/test_concurrent_turns.py` | Needs Docker; cheap but outside a docs batch |
| A5 | Prominent deployment warning (and/or a header-stripping + auth-gateway reference config) | Needs a decision on which gateway the project endorses |
| E1 | Decide: authorize domains per tenant, or carve the ops exception into the constitution | Policy decision, not an engineering one |
| R3 | Correct the Policy contract wording ("no clock") or make `lower` take `now` as an argument | Trivial; bundled with the next change to `security.py` |
| R18 | Measure filter performance; consider Qdrant payload indexes | Needs a realistic corpus |
