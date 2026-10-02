# Contract: Scoping Obligations for Any Store or Tool

**Feature**: [spec.md](../spec.md) | **Matrix of what exists today**: [data-model.md §2](../data-model.md)

**Status**: Retrospective — distilled from `.claude/rules/side-effect-tools.md`, the module
docstrings of every store, and constitution Principle I. This is the checklist a **new**
data-touching component must satisfy, plus the verification each rule currently has.

A store/tool is *tenant-safe* only if **all** of the following hold. The right-hand column says how
each is proven today, so the gaps are visible.

| # | Obligation | Applies to | Proven today by | Gap |
|---|-----------|------------|-----------------|-----|
| S1 | Scope is a **store-native predicate inside the query** (`WHERE tenant = %s`, a Qdrant `Filter`, a RediSearch TAG filter). Never a Python filter over an unscoped result. | every read | unit tests assert the predicate/params are present; real-backend proof for document search (6 concurrent tenants, real Qdrant) and the cache's tenant axis (real Redis Stack) in `tests/agent/test_concurrent_turns.py` | no real-backend proof for memories, relational stores, sessions, the cache's principal axis (A4) |
| S2 | The predicate is built from the **stamped ctx**, passed as a parameter; never from a tool argument or model text. | every read/write | `_ctx_or_refuse` + `args_schema` review | — |
| S3 | **Narrowing inputs AND onto the scope, never replace it** (`doc_ids`, `topic`, `department`, `name_contains`). | filtered reads | `test_sql_store.py::…anded_onto_tenant_never_replacing_it` | Qdrant `HasId`∧tenant semantic asserted in a comment only |
| S4 | A **child table carries its own `tenant`** and a join repeats the predicate. | relational | `postgres-init/08-…`, `15-…`; `get_ticket` | fake-cursor tests prove SQL text only |
| S5 | A **row lookup by id includes the tenant**; another tenant's id returns "none", not the row. | by-id reads | `support/store.py::get_ticket` | — |
| S6 | **Content-addressed ids include the tenant** when tenants share an id space. | shared collections | `test_ingestor.py::…never_collide…` | — |
| S7 | A write's **target identity is derived by code**, never supplied by the model. | writes | `_tool_call_point_id`, `_content_point_id` | — |
| S8 | **Personal data adds the owner** (`owner = principal`); retention is applied **at read time**. | memories, sessions | `test_security.py`, `test_sessions.py` | missing-field range semantic unverified |
| S9 | **Unknown/missing ctx ⇒ refuse, never default.** The tool re-checks even if the entry node did. | every tool | routing + tool tests | — |
| S10 | **Metrics about an isolation event carry no tenant/principal labels**; the structured log may. | deletion, refusals | `agent_memory_deletion_total` | — |
| S11 | State addressed by a **client-chosen key** (`thread_id`) needs an **ownership check on every path that reads or mutates it**. | checkpoint, lock, cancel flag | transcript + pending-approval only | **B2** — send/resume/cancel/worker unchecked |

## Verification recipe for S1–S8 (what the *missing* real-backend tests should do — documents and the cache's tenant axis are already covered by `tests/agent/test_concurrent_turns.py`)

Use `tests/containers.py::ensure_qdrant/ensure_postgres/ensure_redis` (Redis Stack, so RediSearch
exists). Seed **two tenants with overlapping content and two principals in one of them**, then:

1. Qdrant: search as tenant A ⇒ no point with `tenant = B`; memory search as principal P1 ⇒ no point
   with `owner = P2`; a point with no `created_at` is not returned; a point older than retention is
   not returned; a `HasId` of a B point returns nothing for A.
2. Postgres: `query_employees("A")` ⇒ no B rows; `get_ticket("A", <B ticket id>)` ⇒ `None`.
3. Redis: `set` as `(A, P1)`, `get` as `(A, P2)` ⇒ miss; `get` as `(other-co, …)` works without a
   RediSearch syntax error.
4. Sessions: `list_sessions` and `session_belongs_to` per `(tenant, principal, domain)`.

A passing run converts Principle I from "believed" to "proven" for S1–S6, S8.

## Not covered by this contract

Per-person *authorization* of actions inside a tenant (any principal in a tenant holds the same
write capability the approval gate permits), per-domain authorization, and authentication of the
caller are unbuilt (spec *Known gaps*; README Roadmap).
