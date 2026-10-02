# Research: Multi-Domain Composition (Support, Ops, Sales on One Graph)

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-02

**Status**: Retrospective — decisions reconstructed from the code, its comments and `GRAPH_PATTERNS.md` patterns 23, 47,
48 and 50 and the README's *Example domains*. Each entry names its evidence. **No `NEEDS CLARIFICATION` remains.**
R14–R20 (Part D) are *findings* from verifying the as-built system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**. *Alternatives are those the code or its
docs name or argue against; where none is recorded the entry says so rather than inventing one.*

---

## Part A — The composition seam

### R1. A domain is a configuration plus a plugin, bound once per `build_graph()` call

- **Decision**: `AgentManifest` (frozen dataclass: `name`, `system_prompt`, `allowed_tools`) is the part a config file could
  express; `DomainPlugin` (`tools()`, `tool_capabilities()`, `policy()`) is the code it can't. `build_graph(manifest=…,
  domain=…)` consumes them; there is no `if domain == "…"` anywhere in the graph. `should_continue` needs a domain's own
  capability map but LangGraph calls it with only `state`, so it is bound once with `functools.partial`.
- **Rationale**: One pipeline means one proof of every safety property. The manifest is frozen so a deployed graph
  cannot have its prompt or tool list mutated underneath it.
- **Alternatives considered**: forking `build_graph()` per product (rejected: re-proves every property per fork);
  branching on a domain name inside the graph (rejected: the same, plus drift).
- **Evidence**: `app/agent/manifest.py`; `app/agent/graph_build.py::build_graph`; pattern 23.

### R2. The default assistant is itself a domain

- **Decision**: `DEFAULT_MANIFEST`/`DEFAULT_DOMAIN_PLUGIN` wrap the original tools, capability map and policy unchanged.
- **Rationale**: Proof that the single-domain system was always "the default domain", and that existing callers are
  unaffected by the seam. A circular import (`manifest.py` needs `graph.SYSTEM_PROMPT`; `build_graph` needs the manifest
  defaults) is closed by a deferred import inside `build_graph()`'s body.
- **Evidence**: `manifest.py` module docstring; `tests/agent/test_manifest.py::TestDefaultsToTheEcorpDomain`.

### R3. The load-bearing test is a toy second domain

- **Decision**: `tests/agent/test_manifest.py` builds a "widget" domain with a genuinely different `Policy` class and one
  tool the default assistant doesn't have, and proves the unmodified gate treats its tool as `mutating` using *that
  domain's* capability map, that the default assistant's tools are absent from its `ToolNode`, that the default domain's
  declarations are unaffected, and that the leak check uses its prompt.
- **Rationale**: A second domain that merely renames the first proves nothing; one with a different policy and tool proves
  the seam.
- **Evidence**: `tests/agent/test_manifest.py::TestSecondDomainProvesReuse`.

### R4. One registry; one domain per process; the name is validated at both entry points

- **Decision**: `DOMAINS = {"ecorp", "support", "ops", "sales"}` → `(manifest, plugin)`. `resolve_domain` raises with the valid
  names; workers and the chat app resolve `AGENT_DOMAIN` once at start; the API validates `X-Domain` against the same keys (a
  422). The seam deliberately does **not** serve several domains from one process.
- **Rationale**: A typo must never silently serve the wrong assistant (fall-back-to-default was rejected as a "confusing way
  to discover a typo"). Several domains in one process would need per-domain graphs, pools and checkpointers in one runtime —
  a further increment, listed on the Roadmap.
- **Alternatives considered**: a fallback to the default domain (rejected, above); a multi-domain runtime (not built).
- **Evidence**: `app/domains/registry.py`; `tests/domains/test_registry.py`; `app/api/main.py::get_domain`.

---

## Part B — The domains

### R5. The tool list is the sandbox, not a policy check

- **Decision**: A domain's `allowed_tools` is exactly what its plugin returns; `ToolNode` and the routing only know those. The
  support domain therefore has no calculator, note-writing, memory or employee lookup — "that omission, not a Policy check, is
  literally what sandboxed means". The shared `ActionAllowlistPolicy` is the second line inside each tool.
- **Rationale**: The cheapest, strongest restriction on what an assistant can do is not offering it; a tool that does not exist
  cannot be reached by a prompt trick. The policy still fails closed on a missing or malformed identity.
- **Alternatives considered**: expose everything and rely on policy checks (rejected: a bug in a check is a hole; an absent
  tool is not).
- **Evidence**: `app/domains/support/domain.py` docstring; `app/domains/policy.py`;
  `tests/domains/{support,sales}/test_domain.py::TestSandboxing`.

### R6. Skill tools are listed first — a measured fix for a small model

- **Decision**: `skill_tools_first(action_tools, reused_tools, run_subagent)` puts `skill_search`/`use_skill` ahead of every
  action tool.
- **Rationale**: A sales deal-math question never called `skill_search` across many runs, even after three escalating prompt
  rewrites. Swapping *only the order* made the local model call `use_skill` first, 3 of 3 fresh runs, with no prompt change: a
  grammar-constrained small model's tool choice is sensitive to bound-list position. Constitution Principle III requires
  checking for a structural cause before adding prompt text; this is that case.
- **Alternatives considered**: more prompt or docstring text (tried three times; rejected by measurement).
- **Evidence**: `app/agent/tools.py::skill_tools_first` docstring.

### R7. Each domain has its own skill pair and its own delegation tool

- **Decision**: `make_skill_tools("<domain>")` and `make_domain_subagent_tool(domain=…, all_tools=…, tool_capabilities=…)` build
  per-domain objects; the delegation tool is `None` — and therefore not exposed — unless a bundled `AGENT.md` declares that
  domain. A skill without a `domains:` tag stays visible everywhere; a subagent without one stays with the default assistant,
  because its declared tools are only meaningful against one tool universe.
- **Rationale**: A domain-tagged package must never leak into a domain it wasn't written for; an empty menu is not a real tool.
- **Evidence**: `app/domains/*/domain.py`; `tests/domains/*/test_domain.py::TestDomainScopedSubagent`; README *Example domains*
  (detail: feature 007).

### R8. Prompts are constants and carry domain-specific defenses with a stated ceiling

- **Decision**: Each `*_SYSTEM_PROMPT` is a ctx-free constant (Principle VI). Ops and sales added anti-disclosure and scope
  paragraphs, and ops a sandbox boundary, after cloud-judged red-teaming found real leaks (a prompt echoed to a "senior
  auditor", a destructive "wipe temp files" script). Re-verification found two narrower paraphrase gaps and they were *not*
  chased with more prompt text.
- **Rationale**: These are prompt-level mitigations only — the sandbox has no allow/deny list — and are documented as such: a
  ceiling of prompt-only defense on a small model, not a wording bug. A deterministic output-side check remains the better
  next step.
- **Evidence**: `app/domains/ops/domain.py`, `sales/domain.py` docstrings; pattern 48; `promptfoo/ops-redteam.yaml`.

### R9. Domain data lives in its own tenant-scoped tables, with child tables scoping themselves

- **Decision**: Support: `support_tickets` + `support_ticket_comments`. Sales: `crm_leads` (unique `(tenant, contact)`) +
  `crm_followups` + `crm_lead_notes`. Every statement is fixed SQL with `tenant = %s`; a child carries its own `tenant` column
  rather than inheriting it through a join. An append (a comment, a note) became its own keyed row, with the flattened text
  computed at read time (`STRING_AGG`), after replays silently doubled appended text (feature 003).
- **Rationale**: Principle I: scoping inside the query, and a child that scopes itself so a missing join cannot unscope it.
- **Alternatives considered**: appended text columns (replaced); a separate database per domain (rejected: the same
  "this app's own operational data" lifecycle as `employees`/`usage_ledger`).
- **Evidence**: `postgres-init/07, 08, 15`; `app/domains/*/store.py`; `tests/domains/*/test_store.py` (fake cursors — statement
  shape only).

### R10. Ops incidents are global — a deliberate exception, recorded in feature 002

- **Decision**: `ops_incidents` has no tenant column; `opened_by` is the reporting principal. Identity is still required
  ("proves a legitimate caller of this deployment, not a row-scoping filter").
- **Rationale**: The incidents are about the app's *own* operational metrics, which have no tenant dimension; a tenant column
  would be meaningless.
- **Evidence**: `postgres-init/10-ops-incidents.sql` header; `app/domains/ops/tools.py` docstring; feature 002 E1.

### R11. `list_my_tickets` is owner-scoped; `get_ticket` is not — a decision that no longer fits

- **Decision**: The listing filters on tenant **and** requester because there is "no caller-supplied ticket id to trust here";
  the single-ticket lookup, escalation and comment filter on tenant and number only.
- **Rationale**: Written when a ticket number was thought of as the capability a customer already holds. The chat-app channel
  then made every customer a principal of one shared tenant.
- **Alternatives considered**: none recorded. See B7 (R14).
- **Evidence**: `app/domains/support/store.py` (`get_ticket`, `list_tickets_for_requester`, `escalate_ticket`, `add_comment`).

---

## Part C — Scheduled and ad-hoc work

### R12. A cron job can never approve itself, so it must not enter the loop

- **Decision**: `scripts/ops_digest.py` and `scripts/followup_sweep.py` are fixed pipelines: the digest calls the metrics client and
  the team-channel sink directly and uses the model for one plain completion; the sweep calls the sales store and the sink
  directly, drafts one nudge per due follow-up and leaves sending to a human. Neither calls a tool through `ToolNode`.
- **Rationale**: The mandatory gate has no bypass flag. Running a write through the loop would pause with no one to approve;
  auto-declining (as the chat app does) would silently make "post the digest" never happen. The model is reserved for turning
  numbers and notes into prose.
- **Alternatives considered**: the agent loop with an auto-approve flag (forbidden by Principle II); auto-decline (rejected: the
  job would silently do nothing).
- **Evidence**: both scripts' docstrings; pattern 47; constitution Principle II (last bullet).

### R13. The ad-hoc investigation uses the full loop and discloses its limit

- **Decision**: `scripts/ops_investigate.py` builds the ops graph once with an in-memory saver under the local identity and
  returns the last assistant message. A state-changing call pauses; there is no resume path, so the answer is typically empty.
- **Rationale**: An open-ended question wants the domain's *full* toolset, which the narrower delegated sub-assistant lacks. The
  empty answer is "the honest signal a human needs to be in the loop"; the docstring and the README say so.
- **Alternatives considered**: delegating to `run_subagent` (rejected: narrower than an open-ended question needs); adding a
  resume loop (not built).
- **Evidence**: `scripts/ops_investigate.py` docstring; README *Ad-hoc investigation and subagents*. **Untested** (A5).

---

## Part D — Findings (not decisions)

### R14. FINDING B7 — a ticket is addressable by number by any principal of the tenant

- **Observation**: `check_ticket_status` → `store.get_ticket(tenant, id)`, `escalate_to_human` → `escalate_ticket(tenant, id, …)`,
  `add_ticket_comment` → `add_comment(tenant, id, …)`: each statement filters on tenant and id only.
  `list_tickets_for_requester` also filters on requester.
- **Reproduction** (statement level; scratch harness, not in the suite): a fake database that matches rows only on the predicates a
  statement carries; a ticket opened as `telegram:111`; calls made as `telegram:222` in the same tenant → the status call
  returned subject, description and status; `escalate_ticket` and `add_comment` returned `True`; the statements carried no
  requester condition.
- **Consequence**: in the shipped chat-app configuration (one shared tenant, one principal per customer) a customer can read
  another customer's ticket — which holds what they typed to support, e.g. an order or the last digits of a card — and escalate or
  comment on it. Ticket ids are `SERIAL`. Not cross-tenant, so Principle I's tenant rule holds; its owner rule for personal data does
  not. Escalate/comment still pause for approval, but the approver is the unauthorized customer.
- **Options**: add `requester = %s` for customer callers; decide how an authorized agent (a claim in `ctx["claims"]`) is exempt;
  or document that a ticket number is a shared capability within a tenant and deploy one tenant per customer. Left open and
  disclosed.

### R15. FINDING B8 — one failing follow-up aborts the sweep

- **Observation**: `run_followup_sweep` is `for item in due: draft → post → mark done`, with no guard.
- **Reproduction** (scratch harness): three due items, the model raising for the second → the sweep raised; only the first was
  marked done and posted; the third was never handled; the second stays `pending`.
- **Consequence**: a deterministic failure blocks every later follow-up on every run (ordered by due time). Principle V.
- **Options**: guard each item, count and log, continue, exit non-zero at the end. Left open.

### R16. FINDING A1 — two docstrings describe tool sets that no longer exist

- **Observation**: `ops/domain.py`: "Only domain wired to OpenSandbox … Deliberately not wired into support/sales". The registry shows
  all three expose the four sandbox tools; support's and sales' prompts and the README describe them. `support/domain.py`: allowed
  tools "exactly those four plus … five ticket tools"; it also has the external-page reader, the sandbox tools and a delegation tool.

### R17. FINDING A2/A3 — the sweep's ordering and tenant reach

- **Observation**: `post_to_team_channel(...)` precedes `mark_followup_done(...)`; and `__main__` calls `run_followup_sweep()` with the
  default tenant only. The store query is tenant-scoped, so other tenants' follow-ups are never reached.

### R18. FINDING A4 — thresholds are a copy

- **Observation**: `metrics_client.CHECKS` "mirrors alerts.yml's `agent-core-slo` group name-for-name/threshold-for-threshold"; no test
  parses the YAML. Both digest and tool read the copy.

### R19. FINDING A5/A6 — a missing negative test, an untested script, a loose word

- **Observation**: `test_ecorp_only_tools_are_absent` exists for support and sales, not ops; `scripts/ops_investigate.py` has no test;
  `ops_digest.py`'s docstring says "Idempotent, one team-channel post per run".

---

### R20. FINDING A7 — no length bounds on the domain tools' free-form arguments; one categorical argument is free-form

- **Observation**: dumping each domain tool's JSON argument schema shows `type: string` with no `maxLength` on every free-form field — 41 fields across the 31 tools defined in the three domain modules
  (19 domain tools and 12 sandbox wrappers; 28 tools affected); only `due_in_days` has `minimum`/`maximum`. The incident `status` filter is
  `str | None` documented as "open or resolved", not an enumeration. The default assistant's `add_note` (200/4000) and `remember` (2000) declare bounds from named constants.
  A non-blank validator exists on most fields. Constitution Principle III requires length *and* format bounds on free-form fields.
- **Consequence**: an oversized argument is written whole to a row (`TEXT`), the team-channel file and logs, or a webhook body; the model's
  own output ceiling is the only limit.
- **Options**: a `max_length` per field from a named constant; a registry-wide test that every `str` field of every write tool's schema
  has one (it would also cover tools added later). Left open and disclosed.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B7 | Owner-scope the single-ticket functions; decide the authorized-agent exemption | Needs an identity decision; test first |
| B8 | Guard each follow-up in the sweep; exit non-zero at the end | Test first; small |
| A1 | Correct the two docstrings | Docs-only; do first |
| A2 | Accept and disclose the post-then-mark window | Decision |
| A3 | Sweep every tenant with due follow-ups | Needs a store function |
| A4 | A test that `CHECKS` agree with `alerts.yml` | Small |
| A5 | Ops negative test; a thin test of `investigate()` | Small |
| A6 | Reword "idempotent" | Wording |
| A7 | Length bounds on every free-form domain-tool argument, plus a registry-wide test | Needs the numbers (a product decision) |
| — | Several domains in one process; a resume path for the ad-hoc script; a deterministic output-side check for prompt echo | Roadmap / out of scope |
