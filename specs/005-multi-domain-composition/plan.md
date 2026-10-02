# Implementation Plan: Multi-Domain Composition (Support, Ops, Sales on One Graph)

**Branch**: `005-multi-domain-composition` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/005-multi-domain-composition/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

One graph, several assistants. `build_graph(manifest=…, domain=…)` takes an `AgentManifest` (name, system prompt,
`allowed_tools`) and a `DomainPlugin` (`tools()`, `tool_capabilities()`, `policy()`); with neither it uses
`DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN`, which wrap the original assistant unchanged. The only domain-specific code
paths are the manifest's prompt (used for the system message and for the prompt-leak check) and the tool list the
`ToolNode` and routing know. `app/domains/registry.py` maps `"ecorp" | "support" | "ops" | "sales"` to a
`(manifest, plugin)` pair; workers and the chat-app channel resolve `AGENT_DOMAIN` once at start, and the API validates
`X-Domain` against the same registry.

Each example domain is the same three files — `store.py` (fixed, parameterized, tenant-scoped SQL), `tools.py` (typed
tools: `ctx check → idempotent(tool_call_id, fn) → timed impl`) and `domain.py` (prompt, tool list, tier declarations,
policy). Support adds tickets and comments; sales adds leads, follow-ups and notes; ops adds a global incident log, a
Prometheus metrics client and a vendor-status reader. Shared pieces: `ActionAllowlistPolicy`, the team-channel
`notify` sink, and the sandbox/crawl tools (feature 009). Three scripts run outside the agent loop: the ops digest and
the sales follow-up sweep are fixed pipelines (store/metrics/notify calls plus one plain model completion for prose);
`ops_investigate.py` is a one-shot use of the full ops graph.

The plan records honestly that two reproduced defects (**B7**, **B8**) and seven smaller gaps sit around a composition
seam whose *safety* properties — one pipeline, per-domain sandboxes, every write gated and exactly-once — hold where
they are asserted.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `langgraph==0.2.76` (the shared graph), `langchain-core==0.3.86` (`@tool`,
`InjectedToolCallId`), `pydantic` (per-tool `args_schema` with length/format bounds and closed enums),
`psycopg[binary,pool]` (stores), `httpx` (Prometheus client, team-channel webhook), `langchain-openai` (`ChatOpenAI` for
the scripts' plain completions, via the LiteLLM proxy).

**Storage**: Postgres `appdata` — `support_tickets` (+ `support_ticket_comments`), `crm_leads` (unique
`(tenant, contact)`) with `crm_followups` and `crm_lead_notes`, and `ops_incidents` (**no tenant column**);
`postgres-init/07-support-tickets.sql`, `08-crm.sql`, `09-support-ticket-notes.sql` (its `notes` column later dropped),
`10-ops-incidents.sql`, `14-tool-call-id-columns.sql`, `15-append-notes-as-rows.sql`. The team-channel sink appends to
`var/team_channel.log` and, if configured, posts to a webhook. Prometheus is read over HTTP (`PROMETHEUS_URL`).

**Testing**: pytest hermetic tier — `tests/agent/test_manifest.py` (a toy "widget" domain on the unmodified graph),
`tests/domains/test_registry.py`, per-domain `test_domain.py` (sandbox set, scoped delegation, approval gate per write
tool) and `test_store.py` (fake cursors — statement shape only), `tests/domains/ops/test_metrics_client.py`,
`tests/domains/test_write_tools_contract.py` (all 27 write tools, two checks each), `tests/scripts/test_ops_digest.py`
and `test_followup_sweep.py`. **Not tested against a real service**: the stores' `UNIQUE`/`ON CONFLICT` behavior and
tenant predicates (Principle VII known gap). **Not tested at all**: `scripts/ops_investigate.py`, the ops domain's
"default-only tools absent" property, the metrics thresholds against `alerts.yml`, a sweep with a failing item.

**Target Platform**: Linux containers; each domain a separate worker pool (feature 004); scripts under an external
scheduler.

**Project Type**: Domain plugins on a shared graph, plus cron scripts.

**Performance Goals**: None asserted.

**Constraints**: Tool soft timeout 15 s; ops metrics query timeout 10 s; sweep and digest fixed principals
(`sales-followup-cron`, `ops-cron`) in the default tenant; a lead's name is sticky; per-domain tool counts — support 15,
ops 14, sales 16, default 9.

**Scale/Scope**: 4 domains; 27 write tools; 3 scripts; 4 bundled sub-assistants across the three example domains
(feature 007).

**Unknowns**: none — every value is read from the repository.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | **Primary** | **PASS on tenant; FAIL on owner scoping for tickets (B7); ops exception (E1, feature 002)** | Every support/sales statement carries `tenant = %s`; child tables carry their own tenant; every tool checks ctx first (`_ctx_or_refuse` + the allowlist policy). **B7**: `get_ticket`, `escalate_ticket` and `add_comment` filter on tenant and number only — the constitution scopes *personal data* to its owner too, and in the shipped chat-app configuration every customer shares one tenant. Reproduced at statement level. Ops incidents are global by design and noted in feature 002 (E1). |
| II | Mandatory approval (NN) | Yes | **PASS** | Every non-read-only tool is gated (feature 003, now enforced by the contract test). Scheduled jobs are fixed pipelines that never enter the loop — exactly the constitution's carve-out for cron. The ad-hoc script discloses its no-resume limit. Sub-assistants are read-only (feature 007). |
| III | Fixed, typed tools | Yes | **PASS with a reading note and one gap (A7)** | Every tool declares a Pydantic `args_schema` with bounded fields and closed enums (`TicketPriority`); SQL is fixed and parameterized; the sandbox surface is wrapped behind four flat tools. A *created* row's id is derived by code. Tools that act on an *existing* row (`ticket_id`, `incident_id`, a lead `contact`) take that identifier from the model — acceptable only because the statement scopes it; B7 is the case where it does not. **A7**: Principle III also requires length bounds on free-form fields; none of the 41 free-form string arguments across the 31 tools in the domain modules has one, and the incident status filter is not an enum (the default assistant's note and memory tools are bounded). The skill-first tool ordering is the documented structural fix before more prompt text. |
| IV | Exactly-once side effects (NN) | Yes | **PASS (inherited)** | `idempotent()` wraps all 27 write tools (contract test); inserts and appends are keyed rows (`tool_call_id` unique, `ON CONFLICT DO NOTHING`); status updates and lost-lead closing are naturally idempotent. The sweep's post-then-mark order is a duplicate-notification window (A2), a draft for a human, not a write. |
| V | Bounded, observable failure | Yes | **PASS with 1 defect (B8) and 3 advisories** | Team-channel failures are counted (`agent_team_channel_notify_total`) and alerted (`TeamChannelNotifyFailing`). **B8**: the sweep aborts the batch on one failure. A2 (repeat after crash), A3 (default tenant only) and A4 (thresholds copied, unchecked). |
| VI | Untrusted content is data | Yes | **PASS** | Each system prompt is a ctx-free constant; the sweep appends a constant instruction block; leak detection uses the domain's own prompt. Prompt-level defenses in ops/sales are documented as a ceiling (redteam found residual leaks). Server-side fetches (external page, vendor page, lead website) use the shared SSRF guard (feature 009). |
| VII | Test discipline | Yes | **PASS with the known gap and A5** | Hermetic coverage of the seam, each domain's sandbox/approval/stores and both scheduled jobs; stores use fake cursors (statement shape only — not described as proven). Gaps: A5 (ops negative test; `ops_investigate.py`), no sweep-failure test (B8). |
| VIII | Why-first docs, honest gaps | Yes | **PASS with disclosures** | Pattern 23 and the README *Example domains* carry the reasoning and the cron-script rationale; the ad-hoc script discloses its limit in its own docstring. **A1**: two domain docstrings contradict the code; B7, B8 and A2–A6 are not yet in the README Roadmap. |

**Gate result (pre-research)**: no violation of II or IV. **B7 is a Principle I defect** (owner scoping of personal data,
within a tenant) and B8 a Principle V defect. They are *defects*, not justified exceptions; the plan proceeds because
it describes shipped code.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged. Writing the per-store scoping
table in `data-model.md` §3 is what made B7 visible as an *inconsistency inside one store* (the listing is owner-scoped,
the lookup is not) rather than a missing feature, and writing `contracts/scheduled-jobs.md` made B8 a contract
violation (FR-023).

## Project Structure

### Documentation (this feature)

```text
specs/005-multi-domain-composition/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the failures behind each
├── data-model.md                  # Phase 1 — manifest/plugin, per-domain tools, tables, scoping matrix
├── quickstart.md                  # Phase 1 — runnable checks per tier, incl. the B7/B8 reproductions
├── contracts/
│   ├── domain-plugin.md           # the manifest/plugin seam, the registry, the recipe for a new domain
│   ├── domain-tool-surfaces.md    # each domain's tools, tiers, tables and scoping
│   └── scheduled-jobs.md          # the digest, the sweep and the ad-hoc script
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── agent/
│   ├── manifest.py                # AgentManifest, DomainPlugin, DEFAULT_MANIFEST/DEFAULT_DOMAIN_PLUGIN
│   ├── graph_build.py             # build_graph(manifest=, domain=) — the only consumer
│   └── tools.py                   # skill_tools_first, make_skill_tools, the default tool set
├── domains/
│   ├── registry.py                # DOMAINS, resolve_domain
│   ├── policy.py                  # ActionAllowlistPolicy
│   ├── notify.py                  # team-channel sink
│   ├── support/  {store,tools,domain}.py
│   ├── ops/      {store,tools,domain,metrics_client}.py
│   └── sales/    {store,tools,domain}.py
postgres-init/                     # 07, 08, 09, 10, 14, 15
scripts/                           # ops_digest.py, followup_sweep.py, ops_investigate.py
tests/
├── agent/test_manifest.py · domains/{support,ops,sales}/ · domains/test_registry.py · domains/test_write_tools_contract.py · scripts/
```

**Structure Decision**: One package per domain with the same three-file shape, registered in one dict. No new
abstractions beyond the manifest/plugin pair; domains share *functions* (the policy, the notify sink, the reused
read-only tools) not base classes.

## Complexity Tracking

> Filled because the Constitution Check found two defects and several gaps. Defects are listed without a
> justification column: they are simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B7 (defect, open)** — a ticket is addressable by number by any principal of the tenant: read, escalate, comment. Reproduced at statement level. | Not needed — the single-ticket functions were written tenant-only ("no caller-supplied id to trust" justified the *listing* being narrower), before the chat-app channel made every customer a principal of one tenant. | Add a requester predicate to `get_ticket`/`escalate_ticket`/`add_comment` for customer callers, with a decision on how an authorized agent identity (a claim in `ctx["claims"]`) bypasses it; failing test first with two customers in one tenant. Its own PR; needs the agent-identity decision. |
| **B8 (defect, open)** — one failing follow-up aborts the sweep and blocks every later one. Reproduced. | Not needed — written as a simple loop; the per-item failure case was never exercised. | Wrap each item (`except Exception  # noqa: BLE001`), count and log the failure, continue, and exit non-zero at the end if any failed so the scheduler still sees it; failing test first (three items, the second raises). Its own PR. |
| **A1** — two docstrings contradict the code (ops "only domain wired to OpenSandbox"; support's "exactly" tool list). | The sandbox was wired into support and sales after those docstrings were written. | Correct both; docs-only. Constitution Governance. |
| **A2** — the sweep posts then marks done; a crash between repeats the draft. | The post is a best-effort, human-reviewed pivot; marking is a plain update. | Mark first (then a crash drops a nudge) or accept the window and disclose it. Prefer disclosure: a duplicate draft for a human is the cheaper error. |
| **A3** — the sweep covers only the default tenant. | Written for the demo's single tenant. | Iterate the tenants that have due follow-ups (a store function returning distinct tenants), one pass each, with the B8 guard. |
| **A4** — ops thresholds are a copy of `alerts.yml`. | Reading the YAML at runtime adds a dependency on the observability tree being present. | A test that parses `alerts.yml` and asserts every `CHECKS` expression and threshold appears in it. |
| **A5** — no ops "default-only tools absent" test; no tests for `ops_investigate.py`. | Support and sales got the negative test when written; ops was added in a different order. | Copy the support test; add a thin test of `investigate()` with a stub graph. |
| **A6** — `ops_digest.py` says "idempotent". | Loose wording. | Reword to "safe to re-run". |
| **A7** — no length bound on any domain tool's free-form string argument; the incident status filter is a free-form string. | Bounds were added to the default tools' arguments with named constants; the domain tools were written with a non-blank validator only. | Add `max_length` from named constants per field, make the status filter an enum, and add a registry-wide hermetic test that every `str` field of every tool's schema has a bound — extending the A7 contract test over the registry. Picking the numbers is a product decision. |
