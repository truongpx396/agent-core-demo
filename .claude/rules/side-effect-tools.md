---
paths:
  - "app/agent/tools.py"
  - "app/agent/tool_idempotency.py"
  - "app/agent/sql_store.py"
  - "app/domains/**/*.py"
  - "postgres-init/**/*.sql"
---

# Adding or changing a `mutating` / `outward` tool

Constitution Principles I–IV apply. Use `app/domains/support/tools.py::create_ticket` and
`app/domains/support/store.py::create_ticket` as the reference implementation.

## Tool layer (`tools.py`)

1. Declare the tier in that module's `TOOL_CAPABILITIES` (`read_only` / `mutating` / `outward`),
   add the tool to its `TOOLS` list, and — in a domain — to its `*_POLICY`
   `ActionAllowlistPolicy` set. The domain manifest's `allowed_tools` is derived from `TOOLS`.
   An undeclared tool is treated as `outward`; don't lean on that default.
2. Pydantic `args_schema` with bounds, closed `Enum`s, and a `_not_blank` validator. Put
   `tool_call_id: Annotated[str, InjectedToolCallId]` in BOTH the schema and the signature.
3. Body shape, in this order: `_ctx_or_refuse(config, "<tool>")` → return `_NO_CTX_REFUSAL` if
   `None` → `await idempotent(tool_call_id=..., ctx=..., config=..., tool_name=..., fn=lambda:
   _arun_with_timeout(_impl, ...))`. Put the real work in `_<tool>_impl`.
4. Take `tenant`/`principal` from `ctx`, never from an argument the model can set.
5. Never catch the `TimeoutError` from `_arun_with_timeout` and retry in the tool body.
   `idempotent()` turns it into `MutatingToolTimedOut`; a retry there mints a new
   `tool_call_id` that dedup cannot see. Don't add a generic retry around a write.
6. A cron or other unattended job calls the `_impl`/store function directly; it never invokes the
   tool through the agent loop (the approval gate has no unattended bypass, by design).
7. A best-effort pivot send backing an already-committed write (`app/domains/notify.py`) never
   raises, but MUST emit a metric and have an alert (see `runtime-reliability.md`).

## Store layer (`store.py`, `sql_store.py`)

- Fixed SQL, `%s` parameters, and `tenant` in every `WHERE` / `INSERT`. Never interpolate a value.
- Pure INSERT: add `tool_call_id TEXT UNIQUE` (nullable), `... ON CONFLICT (tool_call_id) DO
  NOTHING RETURNING id`, and on no row returned, read the existing row back and return its id.
- An "append" is its own row keyed by `tool_call_id` (see `support_ticket_comments`,
  `crm_lead_notes`). Never concatenate onto a TEXT column; aggregate with `STRING_AGG` on read.
- An UPDATE must be naturally idempotent (`SET col = <constant>`), or document why it isn't.
- Qdrant writes derive the point id from `tool_call_id` / content (`uuid5`), never `uuid4()`.
- `async with get_connection()` is already a transaction (commit on exit, rollback on exception):
  no manual `BEGIN`/`COMMIT` around related writes.

## Schema (`postgres-init/`)

- New numbered file continuing the sequence; prefer it over editing an applied one. The header
  comment says what changes and how an existing volume applies it (init scripts only auto-run on
  a fresh volume), and the README's list of tables is updated.
- A child table carries its own `tenant` column rather than relying only on its parent's.

## Tests and docs

- Store test (fake-cursor style, `tests/domains/support/test_store.py`): tenant appears in the
  params, `tool_call_id` is passed through, and a repeated id returns the original row / is a
  no-op. Note this proves statement shape only — say so rather than claiming constraint coverage.
- Tool test: refuses with no ctx, declares the right tier, and goes through `idempotent()`.
- Add or update the `GRAPH_PATTERNS.md` entry (and "Extending Further" if you closed or left a
  gap).
