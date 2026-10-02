# Data Model: Multi-Domain Composition (Support, Ops, Sales on One Graph)

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

**Status**: Retrospective — read from the code and the SQL. Appdata tables are in the `appdata` database
(`app/agent/sql_store.py::get_connection`).

## 1. The seam

```text
AgentManifest (frozen)                      DomainPlugin (Protocol)
  name: str                                   tools() -> list[BaseTool]
  system_prompt: str   (ctx-free constant)    tool_capabilities() -> {tool_name: "read_only"|"mutating"|"outward"}
  allowed_tools: tuple[str, ...]              policy() -> Policy          # informational: build_graph() never calls it
```

`build_graph(manifest, domain)` stamps the compiled graph with its manifest; `runtime.py::_ensure_seeded_async` reads
`graph.manifest.system_prompt` to seed a new thread with the *domain's* prompt rather than the module-level default. Registry (`app/domains/registry.py`):

| Name | Manifest | Plugin | Tools exposed |
|------|----------|--------|---------------|
| `ecorp` (default) | `DEFAULT_MANIFEST` | `_EcorpDomainPlugin` (wraps `TOOLS`, `TOOL_CAPABILITIES`, `DEFAULT_POLICY`) | 9 |
| `support` | `SUPPORT_MANIFEST` | `_SupportDomainPlugin` (`SUPPORT_POLICY`) | 15 |
| `ops` | `OPS_MANIFEST` | `_OpsDomainPlugin` (`OPS_POLICY`) | 14 |
| `sales` | `SALES_MANIFEST` | `_SalesDomainPlugin` (`SALES_POLICY`) | 16 |

`ActionAllowlistPolicy(actions: frozenset[str])` — `permit(action, ctx)` is true iff the action is in the set **and** the ctx
is valid; `lower()` raises (these domains' data is in Postgres, queried with an explicit tenant predicate, so nothing ever asks
for a Qdrant filter).

## 2. Tool surfaces

Tier in brackets: **R** read-only, **M** mutating, **O** outward. Every M/O tool is wrapped by `idempotent()` (feature 003).

| Domain | Own tools | Reused / shared |
|--------|-----------|-----------------|
| default | `search_docs` R, `calculator` R, `add_note` M, `remember` M, `query_employees` R, `ask_clarification` R, `skill_search` R, `use_skill` R, `run_subagent` R | — |
| **support** | `create_ticket` M, `check_ticket_status` R, `escalate_to_human` M, `list_my_tickets` R, `add_ticket_comment` M, `fetch_external_reference` O | `search_docs`, `ask_clarification`; **own** `skill_search`/`use_skill`/`run_subagent`; sandbox ×4 O |
| **ops** | `fetch_metrics_summary` R, `post_to_team_channel` O, `log_incident` M, `list_recent_incidents` R, `resolve_incident` M, `check_vendor_status_page` O | `ask_clarification`; **own** `skill_search`/`use_skill`/`run_subagent`; sandbox ×4 O (no `search_docs`) |
| **sales** | `log_lead_interaction` M, `schedule_followup` M, `package_lead_brief` R, `handoff_to_human` M, `list_pending_followups` R, `mark_lead_lost` M, `enrich_lead_from_website` O | `search_docs`, `ask_clarification`; **own** `skill_search`/`use_skill`/`run_subagent`; sandbox ×4 O |

Sandbox tools (`run_command_in_sandbox`, `run_python_in_sandbox`, `read_sandbox_file`, `write_sandbox_file`) are always declared
`outward` — OpenSandbox's own annotations are ignored (feature 009). The delegation tool is present only when a bundled
sub-assistant declares the domain. The bound list puts `skill_search`/`use_skill` first.

### 2.1 Argument shapes

Every tool has a Pydantic `args_schema`; `tool_call_id` is injected (never model-supplied). Fields are `str` unless noted. **No string field below has a maximum length, and the sandbox wrappers' `command`, `script`, `path` and `content` are likewise unbounded (A7).** Where a validator exists it only rejects a blank value.

| Tool | Arguments |
|------|-----------|
| `create_ticket` | `subject`, `description` (both non-blank), `priority` ∈ `low|normal|high|urgent` (default `normal`) |
| `check_ticket_status` / `escalate_to_human` / `add_ticket_comment` | `ticket_id: int`; `reason` / `comment` (each non-blank; `ticket_id` is not text) |
| `list_my_tickets` | none |
| `fetch_external_reference`, `check_vendor_status_page` | `url` (fetched under the shared SSRF guard — feature 009) |
| `fetch_metrics_summary` | none |
| `post_to_team_channel` | `channel`, `message` |
| `log_incident` / `list_recent_incidents` / `resolve_incident` | `summary`, `detail?` / `status?` (a free-form string documented as `open` or `resolved`) / `incident_id: int`, `resolution` |
| `log_lead_interaction` | `name`, `contact`, `notes` (all non-blank) |
| `schedule_followup` | `contact`, `due_in_days: int ∈ [0, 365]`, `note` |
| `package_lead_brief`, `list_pending_followups` | `contact` / `contact?` |
| `handoff_to_human` | `contact`, `brief_summary`, `reason` |
| `mark_lead_lost` | `contact`, `reason` |
| `enrich_lead_from_website` | `contact`, `url` |

## 3. Scoping matrix (what each statement filters on)

| Store function | Reads/writes | `tenant` | `requester`/owner | Notes |
|----------------|--------------|:--------:|:-----------------:|-------|
| `support.create_ticket` | INSERT | ✔ | ✔ (sets `requester` = principal) | `tool_call_id` unique, `ON CONFLICT DO NOTHING` |
| `support.get_ticket` | SELECT (+ comments via `LEFT JOIN … AND c.tenant = t.tenant`) | ✔ | **✘** | **B7** — any principal of the tenant |
| `support.list_tickets_for_requester` | SELECT | ✔ | ✔ | the only owner-scoped read |
| `support.escalate_ticket` | UPDATE | ✔ | **✘** | **B7** |
| `support.add_comment` | SELECT then INSERT + UPDATE (one transaction) | ✔ | **✘** | **B7**; row keyed by `tool_call_id` |
| `sales.find_or_create_lead` | UPSERT lead + INSERT note | ✔ | — (shared CRM) | unique `(tenant, contact)`; name sticky |
| `sales.set_lead_status`, `get_lead`, `lead_history` | UPDATE / SELECT | ✔ | — | |
| `sales.add_followup`, `list_pending_followups`, `due_followups`, `mark_followup_done` | INSERT / SELECT / UPDATE | ✔ | — | `due_followups` joins `crm_leads` |
| `sales.mark_lead_lost` | 3 UPDATE/INSERT statements, one transaction | ✔ | — | status + reason note + cancel pending follow-ups |
| `sales.append_lead_note` | INSERT | ✔ | — | keyed by `tool_call_id` |
| `ops.log_incident`, `list_recent_incidents`, `resolve_incident` | INSERT / SELECT / UPDATE | **— (global)** | `opened_by` recorded | deliberate (feature 002 E1) |

The CRM is a shared team resource within a tenant by design; tickets are customer-originated personal data, which is why B7 is a
defect and the CRM's tenant-wide reads are not.

## 4. Tables

`support_tickets` — `id SERIAL PK`, `tenant`, `requester`, `subject`, `description`, `priority` (`low|normal|high|urgent`, default `normal`),
`status` (default `open`), `escalation_reason` null, `tool_call_id TEXT UNIQUE` null, `created_at`, `updated_at`; index on `tenant`.
`support_ticket_comments` — `id`, `tenant`, `ticket_id → support_tickets`, `comment`, `tool_call_id TEXT UNIQUE` null, `created_at`.

`crm_leads` — `id`, `tenant`, `name`, `contact`, `status` (default `new`), `created_at`, `updated_at`; **unique `(tenant, contact)`**.
`crm_followups` — `id`, `tenant`, `lead_id → crm_leads`, `due_at`, `note`, `status` (default `pending`), `created_by`, `created_at`,
`tool_call_id` unique null; partial index on `(tenant, due_at) WHERE status = 'pending'`. `crm_lead_notes` — `id`, `tenant`,
`lead_id → crm_leads`, `note`, `tool_call_id TEXT UNIQUE` null, `created_at`.

`ops_incidents` — `id`, `opened_by`, `summary`, `detail` null, `status` (default `open`), `resolution` null, `created_at`, `resolved_at` null,
`tool_call_id TEXT UNIQUE` null; **no `tenant`**; index on `(status, created_at DESC)`.

Parent `notes` columns (`support_tickets`, `crm_leads`) were dropped when appends became rows (`15-append-notes-as-rows.sql`); text is aggregated
at read time with `STRING_AGG(… ORDER BY created_at)`.

## 5. State machines

```text
ticket   open ──escalate_ticket──▶ escalated                    (no tool closes or reopens a ticket)
lead     new ──handoff_to_human──▶ handed_off                    new/handed_off ──mark_lead_lost──▶ lost (cancels pending follow-ups)
follow-up pending ──followup sweep──▶ done        pending ──mark_lead_lost──▶ cancelled
incident open ──resolve_incident──▶ resolved                    (resolved_at set; no reopen)
```

`escalate_ticket` and `set_lead_status`/`mark_lead_lost`'s status updates are naturally idempotent (they set a fixed end state);
the keyed inserts (ticket, comment, note, follow-up, incident) are the exactly-once rows (feature 003).

## 6. Scheduled-job identities

| Job | Principal | Tenant | Usage record id | Output |
|-----|-----------|--------|-----------------|--------|
| `scripts/ops_digest.py` | `ops-cron` | default | `ops-digest:<date>` | one post to channel `ops-digest` |
| `scripts/followup_sweep.py` | `sales-followup-cron` | default (**only**; A3) | `followup-sweep:<followup id>` | one post per due follow-up to channel `sales-followups` |
| `scripts/ops_investigate.py` | `local:<os user>` | default | (graph usage) | the last assistant message (often empty on a state change) |

Usage is recorded only when the model reports a nonzero token count.

## 7. Team-channel sink

`notify.post_to_team_channel(channel, message)` appends to `var/team_channel.log`, logs a structured line, and — if `SLACK_WEBHOOK_URL`
is set — best-effort POSTs it. It **never raises**; a failed push increments `agent_team_channel_notify_total{sink, outcome}`, alerted by
`TeamChannelNotifyFailing`. It is a saga *pivot*: the write it follows is already committed and is the source of truth.
