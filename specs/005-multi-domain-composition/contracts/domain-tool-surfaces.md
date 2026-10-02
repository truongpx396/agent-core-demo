# Contract: Domain Tool Surfaces (support, ops, sales)

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §2–§5](../data-model.md) | **Write-tool rules**: feature 003
[write-tool-contract.md](../../003-approval-and-exactly-once-writes/contracts/write-tool-contract.md)

**Status**: Retrospective — `app/domains/{support,ops,sales}/tools.py`, `store.py`, `domain.py`. All refusals use the fixed text
*"Refused: no valid tenant/principal context for this request. …"*. Every M/O tool pauses for approval and runs through `idempotent()`.

## Support copilot

| Tool | Tier | Effect | Scope |
|------|------|--------|-------|
| `create_ticket(subject, description, priority)` | M | inserts a ticket with `requester` = the caller's principal; replay returns the first ticket | tenant + requester stamped |
| `check_ticket_status(ticket_id)` | R | returns status, priority, subject, any escalation and the comments, or `No ticket #N found.` | **tenant only — B7** |
| `escalate_to_human(ticket_id, reason)` | M | sets `escalated` + reason, then posts to channel `support-escalations` (the post is best-effort, feature 003 R1); `No ticket #N found to escalate.` if absent | **tenant only — B7** |
| `list_my_tickets()` | R | the caller's own tickets, newest first (limit 10) | tenant **and** requester |
| `add_ticket_comment(ticket_id, comment)` | M | inserts one comment row (keyed by call id) and bumps `updated_at`, in one transaction; absent ticket → `False` | **tenant only — B7** |
| `fetch_external_reference(url)` | O | live-renders a third-party page (feature 009); never writes to the knowledge base | SSRF-guarded |

Also: `search_docs`, `ask_clarification` (reused), its own `skill_search`/`use_skill`/`run_subagent`, and the four sandbox tools (O).
**Absent**: `calculator`, `add_note`, `remember`, `query_employees`.

## Ops bot

| Tool | Tier | Effect | Scope |
|------|------|--------|-------|
| `fetch_metrics_summary()` | R | queries the app's Prometheus for each configured check; flags readings past their thresholds; an unreachable Prometheus yields `None` readings, not an exception | global (the app's own metrics) |
| `post_to_team_channel(channel, message)` | O | appends to the channel log and best-effort POSTs to a webhook | — |
| `log_incident(summary, detail?)` | M | inserts an incident (`open`), attributed to the principal; replay returns the first id | **global, no tenant** |
| `list_recent_incidents(status?)` | R | most recent first (limit 10), optionally by `open`/`resolved` | global |
| `resolve_incident(incident_id, resolution)` | M | sets `resolved`, the resolution and `resolved_at`; `False` if absent | global |
| `check_vendor_status_page(url)` | O | live-reads a vendor's public status page (feature 009) | SSRF-guarded |

Also: `ask_clarification`, its own `skill_search`/`use_skill`/`run_subagent`, and the four sandbox tools (O). **Absent**: `search_docs`, `calculator`,
`add_note`, `remember`, `query_employees` (no test asserts the last four are absent — A5).

## Sales concierge

| Tool | Tier | Effect | Scope |
|------|------|--------|-------|
| `log_lead_interaction(name, contact, notes)` | M | finds or creates the lead (unique `(tenant, contact)`; name sticky) and appends a note row | tenant |
| `schedule_followup(contact, due_in_days 0–365, note)` | M | inserts a `pending` follow-up due in N days, `created_by` = principal; `No lead found …` if no such lead | tenant |
| `package_lead_brief(contact)` | R | assembles status, history, notes and pending follow-ups | tenant |
| `handoff_to_human(contact, brief_summary, reason)` | M | sets status `handed_off`, posts to channel `sales-handoffs`; `No lead found …` if absent (the description calls it "hot" — A1) | tenant |
| `list_pending_followups(contact?)` | R | the pending queue, most imminent first — the whole tenant queue or one lead's | tenant (shared CRM) |
| `mark_lead_lost(contact, reason)` | M | status `lost`, a reason note, and cancels pending follow-ups — one transaction | tenant |
| `enrich_lead_from_website(contact, url)` | O | live-renders the lead's site and records it (feature 009) | tenant; SSRF-guarded |

Also: `search_docs`, `ask_clarification` (reused), its own `skill_search`/`use_skill`/`run_subagent`, and the four sandbox tools (O). **Absent**: `calculator`,
`add_note`, `remember`, `query_employees` (asserted by `test_ecorp_only_tools_are_absent`).

## Cross-cutting

- Every tool body is `ctx check → idempotent(tool_call_id, fn) → timed impl` (15 s; output scrubbed) — enforced for all 27 write tools by
  `tests/domains/test_write_tools_contract.py`.
- Argument bounds: non-blank validators on most free-form fields; **no maximum lengths (A7)**; the incident status filter is free-form.
- Sandbox tool declarations are capped at `outward` in every domain regardless of the sandbox server's own annotations.

## Invariants a change must preserve

1. A customer-originated record is readable and changeable only by its owner or an authorized agent (**not met for tickets — B7**).
2. A child table carries its own tenant.
3. A tool that acts on an existing row validates the row belongs to the caller's scope in the *statement*, not in the prompt.
4. Absent tools stay absent: a domain adding a tool updates its manifest, its tier map and its tests together.
