# Data Model: Skills and Subagents

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

No relational tables are added. State is: two folders of files, one vector collection, an in-process registry and graph cache, and one existing ledger row per delegated run.

---

## 1. Skill file — `skills/<slug>/SKILL.md`

| Part | Rule | On violation |
|------|------|--------------|
| Front block | Must start with a `---` YAML block closed by `---` | file skipped, warning |
| `name` | non-empty string (trimmed) — the catalog key | skipped |
| `description` | non-empty string (trimmed) — what the search embeds and what the model reads | skipped |
| `domains` | optional; a list of non-empty strings; **absent = every product**, including Ecorp | skipped if present but not such a list |
| Body | markdown after the block, trimmed, non-empty | skipped |
| Directory | the folder name is **not** the key; `name` is | — |
| Duplicate `name` | first in sorted directory order wins | later one skipped, warning |
| Missing folder | empty catalog | — |

`SkillRecord(name, description, body, domains: tuple[str, ...] | None, path)` — frozen.

## 2. Subagent file — `subagents/<slug>/AGENT.md`

Same front-block, `name`, `description`, `domains` (list of non-empty strings) and body rules as §1, plus:

| Part | Rule | Meaning when absent |
|------|------|--------------------|
| `tools` | optional list of non-empty strings | "every read-only tool of the delegating product" |
| `model` | optional non-empty string — a proxy model alias | the parent's own alias (`CHAT_MODEL`, `chat`) |
| `domains` | as §1 | **Ecorp only** (the opposite default to skills) |
| Body | becomes the specialist's **system prompt** | — |

`SubagentRecord(name, description, system_prompt, tools | None, model | None, domains | None, path)` — frozen. A non-list `tools`, a non-string `model` or a non-list `domains` skips the file.

## 3. Skills index — Qdrant collection `skills` (`SKILLS_COLLECTION`)

One point per skill, built by `scripts/index_skills.py`:

| Field | Value |
|-------|-------|
| id | `uuid4()` per run (A2 — not deterministic, which is why the collection is recreated first) |
| dense / sparse vectors | embedding of `"<name>: <description>"`; sparse is best-effort (degrades to dense-only) |
| payload | `{text, name, description}` — **never the body** |

The collection is a **rebuildable cache of the disk catalog**; it is not authoritative and carries no tenant data.

## 4. The product-scoped views

Computed on demand, not stored:

| View | Definition |
|------|------------|
| Skills visible to product *P* | `record.domains is None or P in record.domains` — applied to search hits *and* to an exact-name load; a hit whose name is not in the disk catalog is dropped |
| Specialists declared for *P* | `record.domains is None → P == "ecorp"`; otherwise `P in record.domains` |
| Registry for *P* | `{name: (SubagentRecord, resolved_tool_names)}` for every declared specialist |

### Tool resolution — `_resolve_subagent_tools(declared, all_tool_names, capabilities)`

| Input | Result |
|-------|--------|
| `declared = None` | every tool of the product whose capability is `read_only` |
| a declared name `run_subagent` | **always dropped** (no recursion) |
| a declared name not in the product's tools | dropped, warning |
| a declared name whose capability is not `read_only` (or undeclared → `outward`) | dropped, warning — never upgraded |
| all declared names dropped, or `declared = ()` | **empty** — zero tools, never "everything" |

## 5. Shipped catalogs (read from the repository, 2026-10-02)

**Skills (8)** — the "Instructs" column is the set of known tool names that appear in backticks in the body (a mechanical scan; read the file for intent)

| Name | `domains` | Instructs (named in the body) |
|------|-----------|-------------------------------|
| `support-tier1-triage` | `[support]` | `search_docs`, `ask_clarification`, `create_ticket`, `escalate_to_human`, `check_ticket_status`, `list_my_tickets`, `add_ticket_comment` |
| `support-log-triage` | `[support]` | `fetch_external_reference`, `run_python_in_sandbox` (a required tool) |
| `ops-incident-response` | `[ops]` | `fetch_metrics_summary`, `list_recent_incidents`, `log_incident`, `post_to_team_channel`, `resolve_incident` |
| `vendor-incident-postmortem` | `[ops]` | `check_vendor_status_page`, `fetch_metrics_summary`, `log_incident`, `post_to_team_channel`, `run_python_in_sandbox` (a required tool), `run_subagent` |
| `sales-lead-qualification` | `[sales]` | `search_docs`, `ask_clarification`, `handoff_to_human`, `list_pending_followups`, `log_lead_interaction`, `mark_lead_lost`, `package_lead_brief`, `schedule_followup` |
| `deal-economics` | `[sales]` | `enrich_lead_from_website`, `run_python_in_sandbox` (a required tool); names `calculator` only as what it *cannot* do |
| `onboarding-brief` | **none** | `query_employees`, `search_docs` — **B15** |
| `expense-summary` | **none** | `calculator` — **B15** |

**Subagents (5)** — every declared tool resolves; nothing is dropped in any product (checked against each product's real capabilities)

| Name | `domains` | Declared `tools` | `model` |
|------|-----------|------------------|---------|
| `researcher` | none → Ecorp only | `search_docs`, `calculator`, `query_employees` | — |
| `ticket-researcher` | `[support]` | `search_docs`, `check_ticket_status`, `list_my_tickets` | — |
| `lead-researcher` | `[sales]` | `search_docs`, `package_lead_brief`, `list_pending_followups` | — |
| `metrics-researcher` | `[ops]` | `fetch_metrics_summary`, `list_recent_incidents` | — |
| `vendor-history-researcher` | `[ops]` | `list_recent_incidents` | — |

## 6. A delegated run — lifecycle

| Stage | Created | Owner after the stage | Gap |
|-------|---------|-----------------------|-----|
| Call arrives | tool-call id; `{subagent_name, task}` | the parent turn | A1: `task` unbounded |
| Ctx check | — | — | refused without a valid ctx |
| Graph lookup | `_subagent_graph_cache[(product, name)]` on first use: compiled graph + bound client + **its own in-process store** | the **process** (never freed) | **B13** |
| Run identity | `nested_thread_id = <parent thread>:subagent:<name>:<8 hex>` | the store (a new entry per run) | **B13** — nothing deletes it |
| Execute | nested messages `[SystemMessage(prompt + notice), HumanMessage(task)]`; `require_approval = False`; budgets §8 | the store | — |
| End: answer | `SubagentResult(answer, total_tokens, total_cost_usd)` | the tool → parent (`Command`) | — |
| End: ledger | one `usage_ledger` row `(tenant, principal, nested_thread_id, model_alias, total_tokens, cost_usd, resolved_model)` — best-effort, no-op on zero tokens | feature 008 | — |
| End: timeout / exception | counter `outcome` + histogram, then **re-raise** | the parent's tool-error handler | **B14** — no ledger row, no spend entry; the partial state is still in the store |
| After the run | **nothing** | — | **B13** — the run's checkpoints remain |

> The two defects are one gap seen from two sides: **no stage owns what happens to a run's state once it ends**. Fixing B14 means reading that state on the failure path; fixing B13
> means deleting it on every path; the delete must therefore come *after* the read.

### Outcomes

`agent_subagent_run_total{subagent, outcome}`: `completed` (non-empty answer), `budget_exceeded` (empty final answer — a safety net fired), `timeout`, `error`.

## 7. Spend entry and its reducer

`State.subagent_spend: list[tuple[int, float]]` — one `(total_tokens, total_cost_usd)` per completed delegation; the reducer concatenates and treats `None` as a reset (applied by `validate_input` each turn). The parent's budget check adds the sum to its own totals. A **raised** run adds no entry (B14).

## 8. Settings and constants

| Name | Value | Where | `.env.example`? |
|------|-------|-------|-----------------|
| `SKILLS_DIR` | `skills` (relative to the working directory) | `Settings` | **no** (A8) |
| `SKILLS_COLLECTION` | `skills` | `Settings` | **no** |
| `SKILLS_SEARCH_TOP_K` | `1` | `Settings` | **no** |
| `SUBAGENTS_DIR` | `subagents` | `Settings` | **no** |
| `SUBAGENT_TIMEOUT_SECONDS` | `45` | `Settings` | **no** |
| `MAX_SUBAGENT_COST_USD_PER_RUN` | `0.15` | `Settings` | **no** |
| `MAX_SUBAGENT_ITERATIONS` | `6` | constant, `graph.py` | n/a (deliberately not a setting) |
| `MAX_SUBAGENT_TOKENS_PER_RUN` | `4000` | constant, `graph.py` | n/a |
| nested graph-step limit | `MAX_SUBAGENT_ITERATIONS * 2 + 15` | derived | n/a |
| search over-fetch | `max(4 × top-k, 10)` | constant, `tools.py` | n/a |

> **Both directory settings are relative paths.** They resolve against the process's working directory — the repository root host-native, `/app` in the image, where neither folder exists (B16).

## 9. Metrics (feature 008 owns the registry; listed for traceability)

| Metric | Labels | Emitted when |
|--------|--------|--------------|
| `agent_use_skill_without_search_total` | — | `use_skill` rejected for no prior search |
| `agent_skipped_required_tool_total` | — | a final answer skipped a tool a loaded skill named |
| `agent_subagent_run_total` | `subagent`, `outcome` | every run that gets as far as the nested call |
| `agent_subagent_duration_seconds` | `subagent` | every run, including timeouts and errors |
| *(none)* | — | skill search unavailable (A2); a dropped specialist tool (A4); an empty catalog at start (B16) |
