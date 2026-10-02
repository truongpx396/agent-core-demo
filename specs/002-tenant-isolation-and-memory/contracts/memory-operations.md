# Contract: Memory Operations

**Feature**: [spec.md](../spec.md) | **Approval semantics**: feature 003 | **Retrieval**: feature 001 [data-model.md §2](../../001-core-rag-agent-turn/data-model.md)

**Status**: Retrospective — `app/agent/tools.py` (`remember`, `_remember_impl`, `recall_memories`,
`gather_context`), `app/agent/memory.py`, `app/core/security.py`, `app/retrieval/qdrant_store.py`.

Three operations, with deliberately different trust levels.

## 1. Write — the `remember` tool (agent-facing, approval-gated)

| Aspect | Contract |
|--------|----------|
| Name / capability | `remember` — declared **`mutating`** in `TOOL_CAPABILITIES`, therefore always routed through the approval gate; no flag bypasses it |
| Arguments (model-visible) | `content: str` — non-blank, **`max_length = 2000`** (rejected by the schema before any storage) |
| Injected, not model-visible | `tool_call_id` (`InjectedToolCallId`), `config` (carries ctx) |
| Ctx check | `_ctx_or_refuse(config, "write_memory")`; missing ⇒ the refusal text, nothing stored |
| Exactly-once | wrapped in `idempotent(tool_call_id, …)`; point id `uuid5(ns, tool_call_id).hex`, so a replay upserts onto the same point |
| Stored payload | `{text, kind:"memory", tenant, owner, created_at}` — `tenant`/`owner` from ctx, `created_at` = UTC now; **none caller-supplied** |
| Result string | `"Remembered."` |
| Soft timeout | `TOOL_TIMEOUT_SECONDS` (15) ⇒ `MutatingToolTimedOut`; the agent is steered to verify rather than retry (feature 003) |
| Approval event | `approval_required` with `tool_calls: [{name: "remember", args: {content}}]` (see feature 001 turn-event-stream) |
| Unattended callers | auto-declined: no memory is ever written by a fire-and-forget path |

**Not allowed anywhere**: extracting or writing a memory from turn text without this tool.

## 2. Recall — automatic, never model-invoked

| Aspect | Contract |
|--------|----------|
| Trigger | every turn's pre-fetch (`gather_context`), folded in with document hits |
| Filter | `Policy.lower(ctx, "memories")` ⇒ `tenant ∧ kind="memory" ∧ owner=principal ∧ created_at ≥ now − MEMORY_RETENTION_DAYS` |
| Fresh every call | re-evaluated against the **current** ctx; no cache of results |
| Ranking | hybrid search **without** re-ranking (small per-owner set) ⇒ no relevance floor |
| Presentation | numbered `[n] text` with the documents, inside the `<retrieved_document>` frame; so a memory is **data, never instructions**, and is citable |
| Missing ctx | `("", [])` — enrichment never fails the turn |
| Failure | degrades to no memories; counted via `agent_context_retrieval_degraded_total` |
| `recall_memories()` | exists as a function with **no production caller** (test coverage only) |

There is **no model-invokable tool to read or delete memories.**

## 3. Delete — operator capability (`delete_memories`), not agent-facing

```python
async def delete_memories(
    ctx: SecurityCtx, *, memory_id: str | None = None,
    older_than_days: int | None = None, target_principal: str | None = None,
) -> int
```

| Aspect | Contract |
|--------|----------|
| Selector | **exactly one** of `memory_id` / `older_than_days`; both or neither ⇒ `ValueError("delete_memories requires EXACTLY ONE of memory_id or older_than_days")` and `refused` counted |
| Scope | always `tenant = ctx.tenant ∧ kind = "memory" ∧ owner = (target_principal or ctx.principal)` |
| Cross-principal | `target_principal` may name another principal **in the same tenant** (e.g. a departed employee's request); the function does **not** authenticate that entitlement |
| Mechanism | `count_by_filter` then `delete_by_filter` with the same `Filter` (accepted race: a memory written between the two is deleted but not counted) |
| Return | number of memories counted for removal |
| Invalid ctx | `ValueError("a valid ctx is required to delete memories")`; `refused` counted |
| Audit | counter `agent_memory_deletion_total{outcome="deleted"\|"refused"}` — **no tenant/principal labels**; plus structured log `memory_deleted` `{tenant, principal, selector, count}` or the refusal warning |
| Entry point | **none** — no script, endpoint or make target calls it today (plan A3). An operator must write code to use it |

## Retention

`MEMORY_RETENTION_DAYS` (default 365, `Settings.memory_retention_days`). Enforced **at recall**; no
background sweep removes expired points. A memory with no `created_at` is excluded (never
"permanent"). Expired memories remain stored until `delete_memories` removes them.

## Error and refusal summary

| Situation | Result |
|-----------|--------|
| `remember` with blank / >2000 chars | schema validation error before storage |
| `remember` without ctx | refusal text; nothing stored |
| `remember` not approved | nothing stored |
| recall with bad ctx | empty |
| `delete_memories` with both/neither selector | `ValueError`, counted `refused` |
| `delete_memories` with bad ctx | `ValueError`, counted `refused` |
