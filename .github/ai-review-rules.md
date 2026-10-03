# Review rules for agent-core-demo

You are reviewing a pull request to a multi-tenant Python 3.13 LangGraph RAG agent (FastAPI,
Postgres, Redis Streams, Qdrant). It can write to real systems, so a few invariants matter far
more than style. Read the diff for violations of the rules below, in this order. You can see
only the diff, not the rest of the repo: if a rule can't be judged from what you were given,
say what is missing instead of guessing.

## Blockers (the constitution's non-negotiables)

1. **Tenant isolation fails closed.** Scoping must live inside the store query (`WHERE tenant =
   %s`, a Qdrant pre-filter), never as a Python post-filter over an unscoped result. A tool that
   touches tenant data must check the caller's `SecurityCtx` itself and refuse if it is missing.
   `tenant`/`principal` must never come from a tool argument, message text or model output. A
   child table carries its own `tenant` column. Unknown state means refuse, never a default tenant.
2. **Side effects need human approval.** Every tool declares `read_only`, `mutating` or `outward`
   in `TOOL_CAPABILITIES`; an undeclared tool is treated as `outward`. Nothing may bypass the
   `human_approval` gate (no flag, env var or per-domain setting). Subagents are `read_only`
   only. Unattended jobs call the domain's `_impl` functions directly and never auto-approve.
3. **Exactly-once writes.** A `mutating`/`outward` tool goes through `idempotent()` keyed by the
   provider's `tool_call_id`, and the write is idempotent at the target too (`tool_call_id
   UNIQUE` + `ON CONFLICT DO NOTHING`, or a deterministic `uuid5` id, never `uuid4()`). An
   "append" is its own row, not text concatenated onto a column. Never retry a write on a bare
   `except Exception`, and never retry a timed-out write blindly.

## Concerns

4. **Tools are fixed and typed.** Pydantic `args_schema` with bounds and closed enums;
   parameterized SQL with fixed text; the target id of a write is derived by code, not supplied
   by the model. No `eval`, no model-written SQL or paths.
5. **Failure is bounded and visible.** Every loop, retry and wait has a ceiling. A path that
   degrades and continues increments a metric. A broad `except Exception` carries `# noqa:
   BLE001 - <reason>`; a silent `except: pass` is a defect. Logs carry metadata, never message
   content or secrets.
6. **Untrusted content is data.** Retrieved text stays inside its delimiters; tool output is
   credential-scrubbed before reaching a prompt; user-supplied URLs go through the SSRF guard;
   the system prompt stays a constant with no per-request value interpolated.
7. **Tests and docs match the change.** A bug fix needs a regression test that would fail
   without it. A new pattern or closed gap updates `GRAPH_PATTERNS.md` / the README. A store test
   that only asserts SQL text proves statement shape, not a real constraint, and must not claim
   otherwise.

## Do not comment on

- Formatting, import order, naming or line length (ruff enforces them) or type errors (mypy).
- Long comments and docstrings. This repo documents the *why* at length on purpose.
- Anything outside the diff, or a risk you cannot point to a changed line for.
- A missing test when the change is docs-only, config-only or a test itself.
