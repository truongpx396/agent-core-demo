# Contract: A Write Tool (mutating / outward)

**Feature**: [spec.md](../spec.md) | **Source of truth**: `.claude/rules/side-effect-tools.md` | **Per-tool matrix**: [data-model.md §2](../data-model.md)

**Status**: Retrospective — the checklist every `mutating` or `outward` tool satisfies today, plus the
**enforcement status** of each item (the project's rule is prose; only some of it is tested).

Reference implementation: `app/domains/support/tools.py::create_ticket` and
`app/domains/support/store.py::create_ticket`.

## Tool layer

| # | Obligation | How it looks | Enforced by |
|---|-----------|--------------|-------------|
| W1 | **Declare the tier** in the module's `TOOL_CAPABILITIES`; add the tool to `TOOLS` and, in a domain, to its `ActionAllowlistPolicy` set. Undeclared ⇒ `outward`, but do not lean on it. | `"create_ticket": "mutating"` | `tests/agent/test_tools.py::test_every_tool_in_TOOLS_has_a_declared_capability` for the **default** set only; domain sets: only the sandbox tools (`test_every_sandbox_tool_present_is_declared_outward`) |
| W2 | **Typed args**: Pydantic `args_schema` with length/format bounds, closed `Enum`s, a `_not_blank` validator; `tool_call_id: Annotated[str, InjectedToolCallId]` in **both** the schema and the signature. | | convention + tool arg tests |
| W3 | **Body order**: `_ctx_or_refuse(config, "<action>")` → return `_NO_CTX_REFUSAL` if `None` → `await idempotent(tool_call_id=…, ctx=…, config=…, tool_name="<tool>", fn=lambda: _arun_with_timeout(_<tool>_impl, …))`. Real work in `_<tool>_impl`. | | **not enforced (A7)** — audited by script: 15/15 declared tools and the four sandbox tools × 3 domains comply |
| W4 | **Identity from ctx**: `tenant`/`principal` come from `ctx`, never from an argument the model can set. | | convention |
| W5 | **Never catch the `TimeoutError`** from `_arun_with_timeout` and retry in the tool body. `idempotent()` re-raises it as `MutatingToolTimedOut`; a retry mints a new call id dedup cannot see. No generic retry around a write. | | convention + `test_tool_idempotency.py` |
| W6 | An unattended/cron path calls `_impl`/the store directly and never enters the agent loop. | `scripts/ops_digest.py` | convention |
| W7 | A best-effort pivot send backing an already-committed write (`app/domains/notify.py`) **never raises**, emits `agent_team_channel_notify_total`, and has an alert (`TeamChannelNotifyFailing`). | | alert rule |

## Store layer

| # | Obligation | Enforced by |
|---|-----------|-------------|
| S1 | Fixed SQL, `%s` parameters, **`tenant` in every `WHERE`/`INSERT`**. Never interpolate a value. | fake-cursor tests (SQL shape only) |
| S2 | **Pure INSERT**: `tool_call_id TEXT UNIQUE` (nullable) + `ON CONFLICT (tool_call_id) DO NOTHING RETURNING id`; on no row, read the existing row back and return its id. | fake-cursor tests assert the statement text; **no test proves the real constraint** (Principle VII known gap) |
| S3 | **An append is its own row** keyed by `tool_call_id`; never concatenate onto a TEXT column; aggregate with `STRING_AGG` at read. | same |
| S4 | **An UPDATE is naturally idempotent** (`SET col = <constant>`) or documents why not. (`resolved_at = now()` in `resolve_incident` refreshes on replay.) | review |
| S5 | **Qdrant writes derive the point id** from `tool_call_id`/content (`uuid5`), never `uuid4()`. | `tests/agent/test_tools.py` |
| S6 | `async with get_connection()` **is already a transaction** — no manual `BEGIN`/`COMMIT`; do not split one atomic operation across two checkouts. | `sql_store.py` docstring |

## Schema

New numbered `postgres-init/NN-*.sql` continuing the sequence (prefer it over editing an applied one); state in
its header how an existing volume applies it (init scripts only auto-run on a fresh volume); update the README's
table list; a child table carries its own `tenant` column.

## Tests a new write tool must have (the project's rule)

1. **Store test** (fake-cursor style): tenant appears in params; `tool_call_id` is passed through; a repeated id
   returns the original row / is a no-op — and say so honestly: *this proves statement shape only*.
2. **Tool test**: refuses with no ctx; declares the right tier; goes through `idempotent()`.
3. Update `GRAPH_PATTERNS.md` (and *Extending Further* if a gap was closed or left).

**Gap A7 — closed in #68**: item 2 is now enforced for every domain write tool by `tests/domains/test_write_tools_contract.py`, which enumerates the tools from the registry (an unmapped tool counts as `outward`), checks each refuses without a valid identity and routes through `idempotent()` with the injected call id and its own name, and fails a new tool that has no sample arguments with a pointer to this checklist. It pins that tools are *wired*, not that `idempotent()` or each store's uniqueness holds.

## Duplicate story (Constitution "Spec Kit gates")

A spec/plan adding such a tool MUST state: **tier**, **tenant scoping**, and the **duplicate-side-effect story**
(layer 1 + which layer 2 applies, and what remains if layer 1 is bypassed). The table in data-model.md §2 is the
model answer; note the three tools whose layer 2 does not cover the message they send (R1).

## What this contract does not guarantee

- Two *different* call ids for one real-world request both take effect (no business-key rule — by design).
- A replayed write returns the **first** call's stored text, possibly stale.
- Outward actions with no target row are protected only by layer 1, the conversation lock, and reclaim waiting
  longer than a turn can run.
- Exactly-once is per **call id**; it does not make a *restarted turn* safe — that is what "continue, don't
  restart" is for (queue-job-protocol.md).
