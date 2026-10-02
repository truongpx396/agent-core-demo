# Feature Specification: Multi-Domain Composition (Support, Ops, Sales on One Graph)

**Feature Branch**: `005-multi-domain-composition`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — with two reproduced defects, see *Known gaps* B7 and B8

**Input**: User description: "Multi-domain composition (retrospective spec of the as-built system): one unmodified turn pipeline serves several different assistants — the default knowledge assistant and three example products (a customer-support copilot, an internal ops bot, a sales concierge) — each defined by a small configuration plus a plugin of tools, tier declarations and policy; a domain's tool list is its sandbox; each domain keeps its own data in its own store; scheduled work runs as fixed pipelines that never enter the assistant's tool loop; and adding a domain is a recipe, not a fork."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01) from the code, the
> constitution (*Architecture & Technology Constraints → Composition*; Principles I–IV), `GRAPH_PATTERNS.md` patterns
> 23, 48–50 and the README's *Example domains*. It describes what the system does today. **Boundaries.** What a turn
> does is feature 001; identity, ownership and the global ops data are feature 002; the approval gate and the
> exactly-once write rules every domain tool follows are feature 003; routing a request to a domain's worker pool is
> feature 004; the skill and subagent catalogs a domain draws on are feature 007; the sandbox and crawl tools it
> exposes are feature 009. This feature is the **composition seam** and the **three domains built on it**.
> Two defects were found by *reproducing* behavior with hermetic harnesses while writing it (B7, B8); seven further gaps
> were found by reading the code, tests and docs, or by inspecting each tool's argument schema. All are in *Known gaps*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A new use case is configuration plus code in a fixed shape, never a fork of the pipeline (Priority: P1)

The same turn pipeline — moderation, retrieval, the model, the approval gate, quality checks — runs every
assistant the system offers. What differs between them is two small, separate things: a *configuration* (a name,
a system prompt, which tools it may use) and a *plugin* (the tools themselves, a declaration of what each tool
can do to the world, and a policy). The default knowledge assistant is itself just one such pair, which is the
proof that the single-domain system was always "the default domain". A process serves one domain for its life,
chosen at start; a request names the domain it wants; a name nobody registered is refused loudly rather than
quietly served by the wrong assistant.

**Why this priority**: If every new use case forked or branched the pipeline, every safety property (approval,
isolation, exactly-once) would have to be re-proved per fork. One pipeline means one proof.

**Independent Test**: Build the pipeline twice — once with the default pair, once with a toy second domain — and
confirm the second sees only its own tool, pauses for its own write tool, and leaves the first's tool
declarations unchanged. Resolve each registered name and an unregistered one.

**Acceptance Scenarios**:

1. **Given** the default configuration and plugin, **When** the pipeline is built, **Then** it exposes the
   original assistant's tools, prompt and policy unchanged.
2. **Given** a second domain, **When** the pipeline is built with it, **Then** the model can request only that
   domain's tools, a write tool of that domain pauses for approval, and approving it runs it.
3. **Given** a second domain exists, **When** the first domain's tool declarations are read, **Then** they are
   unaffected.
4. **Given** each registered domain name, **When** it is resolved, **Then** the matching configuration and plugin
   come back; **Given** an unregistered name, **Then** resolution fails and lists the valid names.
5. **Given** a worker or chat-app process, **When** it starts, **Then** it serves exactly one domain, fixed at
   start; **Given** a web request, **When** it names a domain, **Then** it is routed to that domain's workers.
6. **Given** a domain's answer is checked for leaking the system prompt, **When** the check runs, **Then** it
   compares against *that domain's* prompt, not the default one.

---

### User Story 2 - A domain's tool list is its sandbox (Priority: P1)

A customer-support assistant has no business doing arithmetic, writing to the shared knowledge base, remembering
facts across sessions or looking up employees — so it simply does not have those tools. The omission is the
boundary: the pipeline's tool executor only ever knows the tools the domain names, so no prompt trick can reach a
tool that isn't there. Every tool that is present declares what it can do to the world, and anything that
changes state or reaches outside is gated on a human decision (feature 003). Each domain also gets its own pair
of skill-lookup tools and its own delegation tool, so a package written for one domain never appears in
another's catalog.

**Why this priority**: The cheapest, strongest restriction on what an assistant can do is not offering it.
Policy checks inside a tool are the second line; the manifest is the first.

**Independent Test**: List each domain's tools. Confirm support and sales expose none of the default
assistant's arithmetic, note-writing, memory or employee tools; confirm every sandbox tool is declared as
reaching outward; confirm a domain's delegation tool offers only that domain's own sub-assistants.

**Acceptance Scenarios**:

1. **Given** the support domain, **When** its tools are listed, **Then** they are knowledge search, its own
   skill pair, clarification, a delegation tool, five ticket tools, an external-page reader and the four sandbox
   tools — and none of calculator, note-writing, memory or employee lookup.
2. **Given** the ops domain, **When** its tools are listed, **Then** they are metrics, incident log/list/resolve,
   a vendor status-page reader, a team-channel post, the sandbox tools, its skill pair, clarification and a
   delegation tool.
3. **Given** the sales domain, **When** its tools are listed, **Then** they are interaction logging, follow-up
   scheduling, a lead brief, human hand-off, a pending-queue listing, lost-lead closing, a website enricher, the
   sandbox tools, knowledge search, its skill pair, clarification and a delegation tool.
4. **Given** any domain's tool that is not read-only, **When** it is requested, **Then** it pauses for approval
   and runs once approved; **Given** a tool with no valid identity, **Then** it refuses before touching a store.
5. **Given** a domain's delegation tool, **When** its menu is read, **Then** it offers only that domain's own
   sub-assistants and is absent altogether if the domain ships none.
6. **Given** the tool list handed to a small model, **When** it is built, **Then** the skill-lookup tools come
   first (a measured fix: list position, not prompt wording, changed whether the model used them).

---

### User Story 3 - Each domain keeps its own data in its own tenant-scoped store (Priority: P1)

Tickets, comments, leads, follow-ups and notes each live in their own tables, always read and written with an
explicit tenant condition in fixed, parameterized queries, never a model-written one. A child table carries its own tenant column
rather than trusting a join. A lead is unique per (tenant, contact); logging an interaction finds or creates it
and records a note row. Closing a lead out sets its end state, records why and cancels its pending follow-ups in
one transaction. Two stores deliberately differ: **incidents are global** operational data with no tenant (feature
002), and **a customer's own-tickets listing is narrower than a single-ticket lookup** (see B7).

**Why this priority**: Tenant isolation is non-negotiable; the stores are where it is enforced for these domains.
They are also where the system's two least-uniform scoping decisions live.

**Independent Test**: Read every statement each store issues; confirm a tenant predicate (except incidents) and
parameterization. Create a ticket as one customer and try to read, escalate and comment on it as another customer
of the same tenant.

**Acceptance Scenarios**:

1. **Given** a ticket, comment, lead, follow-up or note, **When** it is read or written, **Then** the statement
   carries the caller's tenant and is a fixed, parameterized query, and each child table has its own tenant column.
2. **Given** a customer asks for "my tickets", **When** the list is built, **Then** it is scoped to the tenant
   **and** that customer.
3. **Given** a ticket number, **When** a customer asks for its status, **Then** *(intended)* only its requester
   (or an authorized agent) may see it. **As built any principal of the tenant may read it, escalate it and
   comment on it — see B7.**
4. **Given** a lead contact seen before, **When** an interaction is logged, **Then** the existing lead is found
   (no duplicate) and a new note row is added; **Given** a lead marked lost, **Then** its status, a reason note
   and the cancellation of its pending follow-ups commit together.
5. **Given** an ops incident, **When** it is logged, **Then** it is attributed to the reporting principal and has
   no tenant (a deliberate exception, feature 002).
6. **Given** a fresh database, **When** it initializes, **Then** the domain tables are created by numbered scripts
   that continue the existing sequence.

---

### User Story 4 - Scheduled work is a fixed pipeline, never an agent turn (Priority: P2)

Two jobs run unattended on a schedule: the **ops digest** (read the app's own metrics, flag anything past an
alert-matching threshold, write a short plain-language summary, post it to the team channel) and the **sales
follow-up sweep** (for each follow-up due today, draft one nudge in the concierge's voice and put it in front of
a human for review, then mark it done so it is not drafted again). Neither enters the assistant's tool loop: that
loop's approval gate would pause forever with nobody to approve, or an auto-decline would silently make "post the
digest" never happen. They call the domain's own functions directly and use the model only to turn numbers or
notes into prose; the human stays in the loop for anything that goes out. A one-off **ad-hoc investigation** is
the reverse: a person asks the ops assistant a question and the full loop is the right tool — with one disclosed
limit: a state-changing call pauses and this one-shot has no way to resume it.

**Why this priority**: A scheduled job that needs a write and cannot be approved is the classic way to end up
bypassing the gate. The fixed-pipeline rule is how the constitution keeps scheduled writes reviewable.

**Independent Test**: Run each job against a stand-in model and store; confirm the digest posts once per run and
the sweep drafts, posts and marks each due follow-up, and that neither calls a tool through the agent loop.

**Acceptance Scenarios**:

1. **Given** current metrics, **When** the digest runs, **Then** it flags readings past their thresholds, asks the
   model for a 3–6 sentence summary, posts it once to the team channel and records the token usage under a fixed
   automated principal.
2. **Given** follow-ups due, **When** the sweep runs, **Then** each gets one drafted nudge, posted for review, and
   is marked done; **Given** none are due, **Then** it returns without calling the model.
3. **Given** the ad-hoc script, **When** the model decides to call a state-changing tool, **Then** the run
   pauses and the script returns whatever text exists (typically none) — a disclosed limit, not a silent write.
4. **Given** one due follow-up whose draft fails, **When** the sweep runs, **Then** *(intended)* the remaining
   follow-ups are still handled. **As built the sweep stops at the first failure — see B8.**
5. **Given** several tenants with due follow-ups, **When** the sweep runs, **Then** *(intended)* each tenant's are
   handled. **As built only the default tenant is swept — see A3.**

---

### User Story 5 - Each example domain delivers a complete use case (Priority: P2)

**Support copilot**: answers from the knowledge base and the skill catalog, never from general knowledge; opens a
ticket when it cannot resolve something, escalates to a human with a reason, checks status, lists a customer's own
tickets, adds a comment instead of opening a duplicate, reads a customer-linked third-party page live, and parses
a customer-pasted log in an isolated sandbox. **Ops bot**: summarizes the app's own metrics against its alert
thresholds, logs, lists and resolves incidents, reads a vendor status page, posts to the team channel on request,
and computes in a sandbox. **Sales concierge**: logs inbound interactions, schedules follow-ups, assembles a lead
brief, hands a qualified lead to a person, closes dead leads, and enriches a lead from its website. Each ends in a
human: a ticket escalated to an agent, a team-channel post, a lead handed to a rep.

**Why this priority**: These make the composition seam concrete and show each product following the same
conventions end to end — fixed typed tools, tier declarations, tenant scoping, tests — rather than a prompt-only
re-skin.

**Independent Test**: For each domain, walk its headline flow with the model stubbed: the right tools are
offered, write tools pause and run once approved, read-only tools never pause, team-channel tools notify.

**Acceptance Scenarios**:

1. **Given** the support domain, **When** a customer's issue is not resolved by the knowledge base, **Then** the
   assistant opens a ticket and escalates it with a reason; a follow-up detail is a comment, not a second ticket.
2. **Given** the ops domain, **When** an investigation finds a real anomaly, **Then** the assistant records it
   as a durable incident, and resolves it when fixed; posting to the team channel happens only when asked.
3. **Given** the sales domain, **When** a lead is qualified, **Then** a brief is packaged and the lead is handed
   to a person with a team notification; **Given** a lead that is not converting, **Then** it is closed out and
   its follow-ups cancelled.
4. **Given** each domain's prompt, **When** a user tries to extract it or pushes it out of scope, **Then** the
   prompt-level mitigations reduce the likelihood — and are documented as a ceiling, not a guarantee.

---

### User Story 6 - Adding or changing a domain is a recipe, and the recipe is checked (Priority: P3)

A developer adding a domain writes a store, a tool module and a domain module, adds an init script that continues
the numbered sequence, registers the name, adds a worker service and run targets, and writes tests: the registry,
the sandbox boundary, the approval gate per write tool and the store statements. A contract test enumerates every
domain's write tools so a new one cannot skip the safety checklist; documentation names each domain's real tool
set.

**Why this priority**: It ranks last because it concerns future change; it is what keeps the first two stories true
as domains are added.

**Independent Test**: Add a toy domain in a test and confirm no change to the pipeline is needed; run the
contract test and confirm a new write tool without sample arguments fails with a pointer to the checklist.

**Acceptance Scenarios**:

1. **Given** a new domain, **When** it is registered, **Then** no change to the pipeline builder is needed.
2. **Given** a new write tool in any domain, **When** the contract test runs, **Then** it is checked for the
   identity refusal and the exactly-once wrapper.
3. **Given** a domain's documentation, **When** it is read, **Then** it describes the tools the domain exposes
   today. *(Two docstrings do not — A1.)*

---

### Edge Cases

- A domain that ships no sub-assistant does not expose a delegation tool at all, rather than a tool with an empty
  menu.
- The sandbox tools are always present as a fixed set; each call fails gracefully if the sandbox server is not
  reachable right now (feature 009).
- A scheduled job's fixed identity is not a person's: its usage and its team-channel posts are attributable to
  "the job ran".
- The ops domain's identity check proves "a legitimate caller of this deployment", not a row filter, because its
  data has no tenant.
- A lead's name is sticky: it is set by whichever call first created the lead and never updated.
- A follow-up is marked done *after* its draft is posted; a crash between the two repeats the draft on the next run.
- The metric thresholds the ops digest and tool use mirror the alert rules by copy, not by reference.
- A model that offers to do a refused scan "via a delegate" is a known residual of prompt-only defense on a small
  model.

## Requirements *(mandatory)*

### Functional Requirements

**The composition seam**

- **FR-001**: A domain MUST be expressed as a configuration (name, system prompt, allowed tools) plus a plugin
  (the tools, a tier declaration per tool, a policy); the turn pipeline MUST NOT be forked or branched on a
  domain name.
- **FR-002**: The default assistant MUST itself be defined by the default configuration and plugin, so that the
  original single-domain behavior is the default domain.
- **FR-003**: A registry MUST map each domain name to its configuration and plugin, and an unknown name MUST fail
  loudly listing the valid names — at process start and, for a request, as a 422.
- **FR-004**: A worker or chat-app process MUST serve exactly one domain, fixed at start; a request MUST name its
  domain and be routed to that domain's pool (feature 004); a conversation MUST stay on one domain (feature 002).
- **FR-005**: The tools the model can request MUST be exactly the configuration's allowed tools; the omission of a
  tool, not a policy check, is the boundary.
- **FR-006**: A domain's system prompt MUST be a constant with no per-request value, and the answer's
  prompt-leak check MUST compare against that domain's own prompt.

**Domain sandboxes**

- **FR-007**: The support domain MUST expose knowledge search, its own skill pair, clarification, a delegation
  tool, the five ticket tools, the external-page reader and the sandbox tools, and MUST NOT expose calculator,
  note-writing, memory or employee lookup.
- **FR-008**: The ops domain MUST expose metrics, incident log/list/resolve, the vendor status-page reader, the
  team-channel post, the sandbox tools, its skill pair, clarification and a delegation tool, and MUST NOT expose
  the default assistant's arithmetic, note-writing, memory or employee tools.
- **FR-009**: The sales domain MUST expose interaction logging, follow-up scheduling, the lead brief, hand-off,
  the pending listing, lost-lead closing, the website enricher, the sandbox tools, knowledge search, its skill
  pair, clarification and a delegation tool, and MUST NOT expose the default assistant's arithmetic,
  note-writing, memory or employee tools.
- **FR-010**: Every non-read-only tool of every domain MUST declare its tier, check identity first and route its
  work through the exactly-once wrapper (feature 003); the sandbox tools MUST be declared as reaching outward
  regardless of what the sandbox server claims about them.
- **FR-011**: Each domain MUST have its own skill-lookup pair and its own delegation tool, domain-tagged so another
  domain's packages never appear in its catalogs; a domain with no bundled sub-assistant MUST NOT expose a
  delegation tool (feature 007).
- **FR-012**: A domain's bound tool list MUST place the skill-lookup tools first.

**Data**

- **FR-013**: Support and sales data MUST live in tables that carry a tenant column (child tables carry their own),
  and every statement MUST be a fixed, parameterized query with an explicit tenant condition.
- **FR-014**: A customer's own-tickets listing MUST be scoped to the tenant and that customer.
- **FR-015**: Reading a single ticket by number, escalating it, and commenting on it MUST be limited to the
  ticket's requester or an authorized agent. *(Not met — B7.)*
- **FR-016**: A lead MUST be unique per (tenant, contact); logging an interaction MUST find or create the lead and
  append a note row; a lead's name MUST NOT change after creation.
- **FR-017**: Closing out a lead MUST set its end state, record the reason and cancel its pending follow-ups in
  one transaction.
- **FR-018**: Ops incidents MUST be global operational data attributed to the reporting principal, with identity
  required but no tenant filter (a deliberate exception — feature 002).
- **FR-019**: Each domain's schema MUST be created by numbered init scripts continuing the sequence.

**Scheduled and ad-hoc work**

- **FR-020**: Unattended scheduled jobs MUST NOT enter the agent loop; they MUST call the domain's own functions
  directly, use the model only for prose, and leave anything that goes out to a human.
- **FR-021**: The ops digest MUST read the app's metrics, flag readings past alert-matching thresholds, summarize
  in plain language and post once per run, recording usage under a fixed automated principal.
- **FR-022**: The follow-up sweep MUST draft one nudge per due follow-up, post it for human review and mark the
  follow-up done so it is not drafted again; with nothing due it MUST not call the model.
- **FR-023**: A failure on one follow-up MUST NOT abort the sweep for the others. *(Not met — B8.)*
- **FR-024**: The sweep MUST cover every tenant with due follow-ups. *(Not met — default tenant only, A3.)*
- **FR-025**: The ad-hoc investigation MUST run the ops assistant once under the local identity and disclose that a
  state-changing call pauses without a resume path.
- **FR-026**: The ops thresholds MUST agree with the alert rules they mirror, and a change to one MUST be caught.
  *(No check exists — A4.)*

**Tool arguments**

- **FR-029**: Every free-form string argument of a domain tool MUST have a length bound and a non-blank check, and every
  categorical argument MUST be a closed enumeration (Constitution Principle III). *(Met for most non-blank checks and for the ticket
  priority; **not met** for length bounds, and the incident-status filter is a free-form string — A7.)*

**Process**

- **FR-027**: Adding a domain MUST follow the documented shape (store, tools, domain, numbered init script, registry
  entry, worker service, run targets) and MUST ship tests for the registry, the sandbox boundary, the approval gate
  per write tool and the store statements.
- **FR-028**: Each domain's module documentation MUST describe the tools it exposes today. *(Not met — A1.)*

### Key Entities *(include if feature involves data)*

- **Agent Manifest**: A domain's configuration — name, system prompt, allowed tool names.
- **Domain Plugin**: A domain's code — its tools, a tier per tool, a policy.
- **Registry**: Name → (manifest, plugin) for the default, support, ops and sales domains.
- **Support Ticket / Comment**: A customer issue (subject, description, priority, status, escalation reason,
  requester) and its ordered comment rows; both carry a tenant.
- **Lead / Follow-up / Note**: A sales contact unique per (tenant, contact), its scheduled follow-ups (due time,
  note, status) and its interaction notes; each carries a tenant.
- **Incident**: A global operational record (summary, detail, status, resolution, opened by) with no tenant.
- **Scheduled Job**: A fixed pipeline run by an external scheduler under a fixed automated principal.
- **Team-Channel Notification**: A best-effort message after an already-committed write (feature 003 R1).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A second domain runs on the unmodified pipeline: it sees only its own tool, its write tool pauses and
  runs once approved, and the default domain's declarations are unchanged (demonstrated with a toy domain).
- **SC-002**: 100% of the non-read-only tools of all four domains (27 tools, two checks each) refuse without a valid
  identity and route through the exactly-once wrapper with the injected call id and their own name.
- **SC-003**: No domain exposes a tool outside its configuration; support and sales are verified to expose none of
  the default assistant's four excluded tools. *(The ops domain has no such test — A5.)*
- **SC-004**: A domain's skill search and delegation menu never include another domain's packages.
- **SC-005**: A customer can read, escalate or comment on only their own tickets by any route. *(Not met — B7.)*
- **SC-006**: A scheduled job makes zero tool calls through the agent loop, and the digest posts exactly once per run.
- **SC-007**: One failing follow-up does not prevent the remaining due follow-ups from being drafted. *(Not met — B8.)*
- **SC-008**: An unregistered domain name never starts a process or serves a request.
- **SC-009**: Each domain's documented tool set matches its exposed tool set. *(Not met — A1.)*
- **SC-010**: Every free-form argument of every domain tool has a declared length bound. *(Not met: none of the 41 free-form
  string fields across the 31 tools in the three domain modules does — A7.)*

## Assumptions & Known Gaps

**Assumptions**

- Each domain runs as its own worker pool (and its own bot token for the chat app); "several domains from one process"
  is a Roadmap item, not built.
- In the shipped customer-facing configuration every customer of the support copilot is a principal of one shared
  tenant (the default), so tenant scoping alone does not separate customers.
- A scheduler (cron, a systemd timer, a Kubernetes CronJob) is external; nothing in the repository schedules the jobs.
- The sales CRM is a shared team resource within a tenant by design: any principal of the tenant may read its leads and
  follow-up queue.

**Out of scope for this feature**

- The pipeline (001), identity/ownership and the global ops data (002), approval and exactly-once rules (003), routing
  to a pool (004).
- The skill and subagent catalogs themselves (007), observability and cost (008), the sandbox and crawl tools
  (009).
- Orchestrating several domains in one process; a WhatsApp or other chat-app gateway.

**Known gaps (disclosed, with how each was established)**

- **Bug B7 — any principal of a tenant can read, escalate and comment on any ticket in that tenant (reproduced at
  statement level).** `check_ticket_status`, `escalate_to_human` and `add_ticket_comment` take a ticket number from
  the model and call store functions that filter on tenant and number only; only the "my tickets" listing also
  filters on the requester (the ticket's requester is the principal that opened it), and its docstring describes
  the single-ticket lookup's scoping as "tenant-only" and its own as narrower.
  Reproduced with a stand-in database that matches rows purely on the predicates a statement carries: a ticket
  opened by one customer (principal `telegram:111`) was read — subject, description, status — by a different
  customer (`telegram:222`) of the same tenant, and the escalate and comment functions returned success for it; the
  statements carried no requester condition. Ticket numbers are sequential integers, so guessing is trivial. In the
  shipped chat-app configuration all customers share the default tenant, so this crosses customers. It is a
  Principle I question (personal data is to be owner-scoped), not a cross-tenant leak. Escalation and commenting
  still pause for approval, but the approver is the same unauthorized customer. Whether an authorized support agent
  should be able to see any ticket is a policy decision not made in the code. Not fixed here.
- **Bug B8 — one failing follow-up aborts the whole sweep (reproduced at function level).** `run_followup_sweep`
  loops over the due items with no per-item guard. Reproduced with three due follow-ups and a model that raises for
  the second: the sweep raised after handling the first; the third was never drafted and the failing one stays
  `pending`, so a deterministic failure repeats on every run and blocks every follow-up ordered after it.
  Constitution Principle V: a multi-step operation MUST NOT abort a whole batch for one item's failure. Not fixed here.
- **A1 — two module docstrings describe tool sets that no longer exist.** `app/domains/ops/domain.py` says ops is the
  "only domain wired to OpenSandbox" and that support and sales are deliberately not; all three expose the four sandbox
  tools (and the README says so). `app/domains/support/domain.py` says its allowed tools are "exactly" the four reused
  tools plus five ticket tools; it also exposes the external-page reader, the four sandbox tools and a delegation tool.
  The model-facing description of `handoff_to_human` says it marks the lead "hot"; the code sets the status
  `handed_off`. *(Read from the code and the registry.)*
- **A2 — the follow-up sweep can repeat a draft after a crash.** It posts the draft to the team channel and *then*
  marks the follow-up done; a crash in between re-drafts and re-posts it on the next run. The post is best-effort and
  human-reviewed, so no write is duplicated, but it is the notification window feature 003 calls R1. *(Read.)*
- **A3 — the sweep covers only the default tenant.** `run_followup_sweep(tenant=DEFAULT_TENANT)` is called with no
  argument by the script's entry point, and the store query is tenant-scoped, so another tenant's due follow-ups are
  never nudged. *(Read.)*
- **A4 — the ops thresholds "mirror" the alert rules by copy.** `metrics_client.CHECKS` repeats the expressions and
  thresholds of `observability/prometheus/alerts.yml`; no test compares them, so they can drift apart. *(Read from the
  code and tests.)*
- **A5 — the ops domain has no "default-assistant-only tools are absent" test, and `ops_investigate.py` has no tests.**
  Support and sales each have one; ops exposes none of those tools today but nothing would fail if it started to. The
  one-shot investigation script, which uses the full agent loop, is untested. *(Read from the tests.)*
- **A6 — `ops_digest.py` calls itself "idempotent".** Each run posts a new digest; "safe to re-run by hand" is true,
  "idempotent" is not. *(Read; wording only.)*
- **A7 — none of the domain tools' free-form string arguments has a length bound, and one categorical argument is not a
  closed enum.** Principle III: "free-form fields MUST have length and format bounds" and "categorical fields MUST be
  closed enums". The default assistant's `add_note` and `remember` have `max_length` from named constants. In the three
  domain modules there are 31 tool definitions (19 domain tools plus 12 sandbox wrappers); 28 of them have at least one
  string field with no maximum (41 fields in all: `subject`, `description`, `comment`, `summary`, `detail`, `message`,
  `channel`, `name`, `contact`, `notes`, `note`, `resolution`, `reason`, `brief_summary`, `url`, `command`, `script`, `path`,
  `content`, `status`). Most have a non-blank validator; only `due_in_days` is bounded (0–365). The incident `status`
  filter (`list_recent_incidents`) is `str | None` documented as "open or resolved" rather than an enumeration (it is
  parameterized, so an unknown value just matches nothing). *(Established by dumping every tool's argument schema and by
  `grep` for `max_length` under `app/domains` — no hits.)* An oversized argument becomes an oversized row, log line or
  webhook body; the per-turn token ceiling is the only indirect limit.
