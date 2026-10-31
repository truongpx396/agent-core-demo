# Specs

Spec Kit features, one directory each (`/speckit-specify` numbers the next from the highest `NNN-*` found here: 020). 001-009 and 014-020 are the agent app's; 010-013 are the billing
package's and live in [`packages/billing/specs/`](../packages/billing/specs/), with a one-file pointer left here so their numbers stay taken.

## Reading an older spec: paths have moved

A spec records the paths of the day it was written, and `app/agent/` was reorganised afterwards. The mapping, for any `app/agent/<name>.py` in 001-009:

| Was | Is |
|---|---|
| `graph.py` | `graph/core.py` |
| `graph_<x>.py` | `graph/<x>.py` (`graph_skills` is `graph/skill_guards.py`, `graph_tools` is `graph/tool_guards.py`) |
| `runtime.py`, `runtime_stream.py`, `runtime_legacy_stream.py` | `runtime/core.py`, `runtime/stream.py`, `runtime/legacy_stream.py` |
| `sessions.py` | `runtime/sessions.py` |
| `tools.py` | `tools/builtin.py` |
| `skills.py`, `subagents.py` | `tools/skill_catalog.py`, `tools/subagent_catalog.py` |
| `subagent_tools.py`, `subagent_domain_tools.py` | `tools/run_subagent.py`, `tools/run_subagent_domain.py` |
| `budgets.py`, `budget_holds.py`, `budget_policies.py`, `spend.py`, `pricing.py`, `metering.py`, `usage_events*.py`, `usage_reconcile.py` | the same names under `cost/` |
| `sql_store.py`, `gateway.py`, `moderation.py` | the same names in `app/core/` |
| `model_resolver.py` | `app/adapters/litellm/model_catalog.py` |
| `manifest.py` (`AgentManifest`, `DomainPlugin`) | `app/ports/domains.py` |
| `app/core/serper.py`, `app/domains/notify.py`, `app/domains/sandbox_tools.py`, `app/core/classification.py` | `app/adapters/web/serper.py`, `app/adapters/notify/team_channel.py`, `app/adapters/mcp/sandbox_tools.py`, `app/adapters/mlservice/classification.py` |
| `tool_idempotency.py` | `app/core/tool_idempotency.py` |
| `_arun_with_timeout`, `_run_with_timeout`, `_ctx_or_refuse`, `_ctx_from_config`, `_NO_CTX_REFUSAL`, `TOOL_TIMEOUT_SECONDS` (in `tools.py`) | `arun_with_timeout`, `run_with_timeout`, `ctx_or_refuse`, `ctx_from_config`, `NO_CTX_REFUSAL`, `TOOL_TIMEOUT_SECONDS` in `app/core/tool_runtime.py` |
| `memory.py` | `app/retrieval/memory.py` |

Tests moved the same way: `tests/agent/test_<x>.py` is under `tests/agent/{cost,graph,runtime,tools}/` next to its module's package (`test_sql_store` is in `tests/core/`, `test_memory` in `tests/retrieval/`,
`test_gateway`, `test_moderation`, `test_model_resolver` and `test_tool_idempotency` in `tests/core/`).
