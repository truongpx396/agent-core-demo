# Feature Specification: Skills and Subagents

**Feature Branch**: `007-skills-and-subagents`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — with four reproduced or established defects, see *Known gaps* B13–B16

**Input**: User description: "Skills and subagents (retrospective spec of the as-built system): the assistant can find a packaged, multi-step procedure for a kind of task by meaning and load only that one procedure's instructions; it can hand a self-contained lookup to a specialist that works in its own isolated context with its own small budget and only read-only abilities, and receives only the specialist's answer; procedures and specialists are plain files authors add by editing a folder; each is offered only in the product it was written for; a specialist can never change anything, never needs a human's approval, and can never start another specialist."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01) from the code,
> `GRAPH_PATTERNS.md` patterns 45, 46 and 50, the constitution's Principle II and *Composition* constraint, and the
> tests named in `quickstart.md`. It describes what the system does today. **Boundaries.** The graph loop these tools run
> inside, the approval gate that makes "read-only" mean something, and budgets are features 001 and 003; which tools a
> product exposes is feature 005; the usage ledger and cost governance that a delegated run reports into are feature 008;
> the sandbox a skill may instruct the model to use is feature 009. This feature is the **two capability catalogs and the
> two tool pairs that expose them**: `skill_search`/`use_skill` and `run_subagent`. Four defects were found by *reproducing*
> behavior or by inspecting the built container image while writing it (B13–B16); eight further gaps were found by reading
> the code, tests and deployment files. All are in *Known gaps*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The assistant finds and follows a packaged procedure for a task (Priority: P1)

A person asks for something that has a house procedure — "put together an onboarding brief for a new hire", "total these
expense lines", "triage this support request". The assistant first searches a small catalog of procedures **by meaning**,
sees the best match's name and one-line description, loads **only that procedure's** full instructions, and then follows them
using its ordinary tools. A turn that needs no procedure pays only for the two tool descriptions, not for every procedure's
text. If nothing matches, the assistant is told to carry on without one.

**Why this priority**: This is how the product encodes "the way we do this" without bloating every turn's context or hand-picking
a fixed subset per deployment.

**Independent Test**: Ask for a task matching a bundled procedure; confirm the assistant searches, then loads that procedure by
its exact name, then uses the tools the procedure names. Ask for something unrelated; confirm no procedure is loaded.

**Acceptance Scenarios**:

1. **Given** a task matching a procedure's description, **When** the assistant searches the catalog, **Then** it gets that
   procedure's name and description (by default one candidate) and no other procedure's text.
2. **Given** a procedure name returned by search, **When** the assistant loads it, **Then** it receives the full instructions
   exactly as they are on disk — never a copy from the search index.
3. **Given** a search with no good match, **When** it returns, **Then** the assistant is told to proceed with its other tools.
4. **Given** the assistant tries to load a procedure **without having searched first in the same turn**, **When** the call is
   made, **Then** it is rejected without pausing, the assistant is told to search first (and not to guess a name), and the
   turn continues.
5. **Given** a loaded procedure that names a required tool (today: the Python sandbox), **When** the assistant has not yet
   called that tool this turn, **Then** it is reminded before its next generation, and a final answer that skipped it is sent
   back for correction.
6. **Given** a name that matches no procedure (or one not offered in this product), **When** it is loaded, **Then** the
   assistant gets a clear "no such skill — search first" message, not an error.
7. **Given** the search index has never been built, **When** the assistant searches, **Then** it gets an actionable message and
   carries on; the turn does not fail.

---

### User Story 2 - A procedure or specialist is offered only in the product it was written for (Priority: P1)

Three products share one graph (feature 005). A procedure or specialist written for the support product is never searchable,
loadable or delegable from the sales or ops product — even if the assistant guesses its exact name — because each product's
tools are a different, deliberately narrow set and instructions naming tools the product lacks would mislead it.

**Why this priority**: The constitution's *Composition* constraint says skills and subagents "are domain-tagged so they never
leak across domains". A leak is not a security hole here (specialists are read-only) but it is a correctness one: the assistant is
told to call tools it does not have.

**Independent Test**: In each product, search and load every bundled procedure and list the specialist menu; confirm each
product sees only its own tagged items plus any deliberately shared ones. *(Not met for two shipped skills — B15.)*

**Acceptance Scenarios**:

1. **Given** a procedure tagged for one or more products, **When** another product searches or loads it, **Then** it is invisible
   in search results and refused on an exact-name load.
2. **Given** a procedure with no tag, **When** any product searches, **Then** it is visible (the default that predates tags).
3. **Given** a specialist with no tag, **When** products build their menus, **Then** it is offered to the general (Ecorp) product
   **only** — the opposite default, because a specialist's declared tools only make sense against one tool set.
4. **Given** a search index entry for a procedure that has since been deleted or renamed on disk, **When** a search returns it,
   **Then** it is dropped (disk is authoritative).
5. **Given** a product with no specialist declared for it, **When** its tools are built, **Then** it gets **no** delegation tool at
   all rather than one with an empty menu.
6. **Given** two products, **When** their delegation tools are built, **Then** each has its own menu type; one product's menu never
   contains the other's names.
7. **Given** the shipped procedures, **When** any product lists what it can see, **Then** every procedure it sees only names tools
   that product actually has. **As built, two shipped procedures are visible in all four products although three of them lack the
   tools those procedures name — see B15.**

---

### User Story 3 - The assistant hands a self-contained lookup to an isolated, read-only specialist (Priority: P1)

For a multi-step lookup whose intermediate steps should not clutter the conversation, the assistant picks a specialist from a
short menu embedded in the tool's description and gives it a self-contained task. The specialist works in its **own** context —
it sees its own instructions and that one task, never the conversation — using only the **read-only** tools it was declared to
have, and returns a short answer that the assistant folds into its reply. Delegating never asks the person for approval, because
nothing a specialist can do changes anything.

**Why this priority**: It keeps long lookups out of the main thread and lets a specialist have a focused prompt, without opening a
second route to side effects.

**Independent Test**: Delegate a lookup in each product; confirm the specialist's only message is the task, its tools are all
read-only, the parent receives only the final answer, and no approval pause occurred.

**Acceptance Scenarios**:

1. **Given** a task and a specialist name from the closed menu, **When** the assistant delegates, **Then** a fresh run starts whose
   only inputs are the specialist's own instructions and the task.
2. **Given** a specialist that declares a tool that is unknown, or is not read-only, **When** the registry is built, **Then** that
   tool is dropped with a warning and never upgraded.
3. **Given** a specialist that declares no tools, **When** the registry is built, **Then** it receives every read-only tool of the
   delegating product; **given** a specialist whose declared tools all got dropped, **Then** it runs with **zero** tools — never the
   full set.
4. **Given** any specialist, **When** its tool set is resolved, **Then** the delegation tool itself is always removed — a specialist
   cannot start a specialist.
5. **Given** a delegation call, **When** the graph routes it, **Then** it goes straight to execution (never to the approval gate),
   whether or not the optional approval flag is on.
6. **Given** a call with no identity context, **When** it is made, **Then** it is refused; **given** one with an identity, **Then**
   that identity is passed through to the specialist unchanged (so every tool it calls is tenant-scoped exactly as the parent's are).
7. **Given** a blank task, **When** the call is validated, **Then** it is rejected.
8. **Given** a specialist's answer, **When** it is returned, **Then** it is credential-scrubbed, and the specialist was told not to
   use bracketed citation markers (those belong to the parent's own retrieved context).
9. **Given** the specialist reaches a safety budget before answering, **When** the run ends, **Then** the parent receives a clear
   "did not produce a final answer before hitting one of its own safety budgets" message, not an empty string.

---

### User Story 4 - Delegated work is bounded, accounted for and visible (Priority: P2)

A delegated run is small by construction and cannot run away: a handful of steps, a few thousand tokens, a small cost ceiling and
a wall-clock timeout. Whatever it spends counts against the same turn that asked for it and is recorded for the tenant; an
operator can see how often each specialist completes, times out, errors or runs out of budget, and a trace shows its steps nested
under the parent's. Its memory is released when it ends.

**Why this priority**: Constitution Principle V — every loop and wait is bounded and every degrade path is visible — applied to the one
feature that starts a second agent loop inside a tool call.

**Independent Test**: Run a delegation that finishes, one that exceeds its step budget, and one that times out; compare the outcome
counters, the usage recorded, the parent's remaining budget and the process's retained memory before and after.

**Acceptance Scenarios**:

1. **Given** a run, **When** it executes, **Then** it is capped at 6 agent steps, 4000 tokens, a $0.15 cost ceiling and 45 seconds
   (the last two configurable), with a graph-step limit derived from the step cap.
2. **Given** a completed run, **When** it returns, **Then** its tokens and cost are folded into the parent turn's running totals
   (so two parallel delegations together can exhaust the parent's budget) and reset at the start of the next turn.
3. **Given** a completed run, **When** it returns, **Then** its tokens are recorded to the usage ledger for the tenant under a derived
   thread id `<parent>:subagent:<name>:<8 hex>`.
4. **Given** any run, **When** it ends, **Then** an outcome counter (`completed`, `budget_exceeded`, `timeout`, `error`) and a duration
   histogram are updated, and a log line carries metadata only.
5. **Given** a tracing callback on the parent, **When** a run executes, **Then** its LLM and tool spans nest under the parent's, and
   its own reasoning tokens never appear in the client's answer stream (its tool activity is surfaced, tagged with the specialist).
6. **Given** concurrent delegations (including two in one turn), **When** they run, **Then** they never cross-wire answers, spend or
   threads.
7. **Given** a run that **times out or errors**, **When** it ends, **Then** *(intended)* the tokens it spent before failing are still
   recorded for the tenant. **As built they are not — see B14.**
8. **Given** a finished run, **When** it ends, **Then** *(intended)* nothing retained for it remains in the process. **As built the
   compiled nested graph keeps a copy of every run's conversation for the life of the process — see B13.**

---

### User Story 5 - Authors add procedures and specialists by editing files, and every deployment gets them (Priority: P3)

An author adds a procedure by creating `skills/<name>/SKILL.md` or a specialist by creating `subagents/<name>/AGENT.md`, with a
short header (name, description, optional product tags, and for specialists optional tool list and model alias) and a body. A
malformed file is skipped with a log line and never takes the rest of the catalog down. After the author runs one command the
search index reflects the change, and every deployment — host-native or containerized — serves the same catalogs.

**Why this priority**: The value of a catalog is that non-engineers can extend it; the failure mode is a catalog that silently
doesn't ship.

**Independent Test**: Add a valid, a malformed and a duplicate-named file; confirm one catalog entry, two skips with log lines, and
the others unaffected. Then start the *containerized* stack and confirm the same bundled entries are present. *(Not met — B16.)*

**Acceptance Scenarios**:

1. **Given** a valid file, **When** the catalog loads, **Then** it appears keyed by name; a missing frontmatter block, missing or blank
   name/description, empty body, invalid YAML, a non-list `domains`/`tools`, or a non-string `model` makes that file — and only that
   file — skipped with a warning.
2. **Given** two files with the same name, **When** the catalog loads, **Then** the first (directory order) wins and a warning is logged.
3. **Given** a missing catalog directory, **When** it loads, **Then** the catalog is empty — no error.
4. **Given** the author runs the indexing command, **When** it completes, **Then** the search index holds one `name: description` entry per
   skill and **never** the body.
5. **Given** a bundled catalog, **When** the product is started in **any** supported way, **Then** the same catalog is present. **As built
   the container images contain neither directory — see B16.**
6. **Given** an added or edited file, **When** a *running* process serves its next request, **Then** *(intended)* the change is visible
   after one documented step. **As built re-indexing is not enough; every running process must also be restarted — see A3.**

---

### Edge Cases

- Adding a specialist requires a process restart: its menu is a closed enum built when the tools module is first imported (disclosed in
  pattern 46); `reload_subagents()` re-reads the files but cannot rebuild the enum.
- `skill_search` over-fetches (4× the top-k, minimum 10) from the index, filters by product on the disk catalog, then truncates — so
  results stay correct when the best semantic matches belong to another product.
- The default search returns **one** candidate (`SKILLS_SEARCH_TOP_K=1`) because a distractor alongside the right match made the small
  local model hedge into "none of these"; this trades a second chance for confident commitment.
- `skill_search`/`use_skill` lead the bound tool list on purpose (pattern 50): list position, not wording, decided whether the small local
  model used them at all. `run_subagent` shares that leading tier (reasoned, not independently live-verified — the code says so).
- `use_skill` needs no tenant context: skills are a bundled capability, like the calculator. A tenant-authored catalog is an acknowledged,
  unbuilt extension (pattern 45).
- A specialist's nested run has no way to be resumed hours later, which is *why* it may only use read-only tools (pattern 46) — a paused
  nested write would be an unresumable gate or a bypass.
- The nested run never reads or writes the shared semantic cache, never suggests follow-ups and never compacts history (each would be a
  cross-talk risk or unreachable under its budget).

## Requirements *(mandatory)*

### Functional Requirements

**Skill catalog and search**

- **FR-001**: A skill MUST be one directory under the skills folder holding `SKILL.md`: a YAML header with a non-empty `name` and
  `description`, an optional `domains` list of non-empty strings, and a non-empty body. An absent `domains` MUST mean "every product".
- **FR-002**: A file that does not match FR-001 MUST be skipped with a logged warning and MUST NOT affect any other entry; a duplicate name
  MUST keep the first and warn; a missing folder MUST yield an empty catalog.
- **FR-003**: `use_skill` MUST return the body read from disk by exact name; the search index MUST hold only `{name, description}`, never a body.
- **FR-004**: `skill_search` MUST query the dedicated skills index (over-fetching), keep only hits whose skill exists on disk **and** is
  visible to the calling product, truncate to the top-k (default 1), and return `- name: description` lines; no visible hit MUST return a
  "proceed without a skill" message; a missing index MUST return an actionable message and MUST NOT raise.
- **FR-005**: `use_skill` MUST refuse (with a clear message, not an exception) a name that is unknown **or** not visible to the calling product.
- **FR-006**: Skill tools MUST NOT require an identity context, MUST be time-bounded, and MUST return credential-scrubbed text.
- **FR-007**: The two skill tools MUST sit first in every product's bound tool list (pattern 50); the delegation tool, where present, joins
  that leading tier.

**Skill-misuse guardrails**

- **FR-008**: A batch calling `use_skill` when `skill_search` has not been called this turn MUST be rejected with exactly one tool message per
  call telling the assistant to search first and not to guess; it MUST NOT pause, MUST be counted, and MUST NOT lose any pending call.
- **FR-009**: When a skill loaded this turn names a required tool from the explicit allowlist (today `run_python_in_sandbox`) and that tool
  has not been called this turn, the assistant MUST be reminded before its next generation, and a final answer that still skipped it MUST be
  rejected for correction and counted.

**Subagent catalog and registry**

- **FR-010**: A subagent MUST be one directory under the subagents folder holding `AGENT.md`: a header with non-empty `name` and
  `description`, optional `tools` (list of non-empty strings), `model` (non-empty string) and `domains` (list of non-empty strings), and a
  non-empty body used as its system prompt. FR-002's skip/duplicate/missing-folder rules apply.
- **FR-011**: A subagent with no `domains` MUST be declared only for the general (Ecorp) product; a tagged one only for its named products.
  A product with no declared subagent MUST get no delegation tool.
- **FR-012**: A subagent's effective tool set MUST be its declared tools (or, if none declared, every read-only tool of the delegating
  product) minus any name that is unknown or not read-only (each dropped with a warning, never upgraded) and minus the delegation tool
  itself unconditionally; an empty result MUST mean zero tools, never "everything".
- **FR-013**: `run_subagent` MUST take a `subagent_name` drawn from a closed enum whose descriptions are embedded in the schema and a `task`
  that is not blank; each product MUST have its own enum type and tool object.
- **FR-014**: `run_subagent` MUST be declared `read_only`, MUST route to execution without an approval pause whether or not the optional
  approval flag is set, and MUST NOT be reachable from inside any nested run.

**Delegated run**

- **FR-015**: A run MUST refuse without a valid identity context and MUST pass that context to the nested run unchanged.
- **FR-016**: A run's input MUST be exactly its own system prompt (plus a notice not to use `[n]` citation markers) and the task as the sole
  human message — never the parent's history; it MUST use the specialist's `model` alias if declared, else the parent's.
- **FR-017**: A run MUST be capped at 6 agent steps, 4000 tokens, a configurable cost ceiling (default $0.15) and a configurable whole-run
  timeout (default 45 s), with a graph-step limit derived from the step cap; it MUST use no semantic cache, no follow-up suggestion and no
  history compaction.
- **FR-018**: A run's final answer MUST be credential-scrubbed and returned as the tool result; an empty final answer MUST become the
  explicit "did not produce a final answer … safety budgets" message and be counted as `budget_exceeded`.
- **FR-019**: A run's tokens and cost MUST be folded into the parent turn's running totals through a concurrency-safe reducer that is reset
  at the start of each turn, and its tokens MUST be recorded to the tenant usage ledger under `<parent thread>:subagent:<name>:<8 hex>`.
- **FR-020**: Each run MUST increment `agent_subagent_run_total{subagent,outcome}` with `completed`, `budget_exceeded`, `timeout` or `error`,
  and observe `agent_subagent_duration_seconds{subagent}`; logs MUST carry metadata only.
- **FR-021**: The parent's tracing callbacks MUST be threaded into the nested run; the nested run's events MUST be tagged with the specialist and
  parent thread, its token stream MUST NOT reach the client's answer, and its tool activity MUST be surfaced tagged.
- **FR-022**: Concurrent runs MUST NOT share a thread, state or answer; the compiled nested graph MAY be reused per (product, specialist) provided
  each run is isolated by its own thread id.

**Gaps stated as requirements (not met today)**

- **FR-023**: When a run ends — completed, over budget, timed out or errored — nothing retained for that run's conversation MUST remain in the
  process. *(Not met — B13.)*
- **FR-024**: Tokens spent by a run that times out or errors MUST still be recorded to the tenant ledger. *(Not met — B14.)*
- **FR-025**: A skill or specialist MUST NOT be offered in a product whose tools it instructs the assistant to use but which lacks them.
  *(Not met — B15.)*
- **FR-026**: Every supported way of running the product MUST contain the bundled skills and subagents, and a start with an empty catalog MUST be
  visible (a log line and a metric), not silent. *(Not met — B16, A2.)*
- **FR-027**: The assistant-supplied `query`, `name` and `task` MUST have length bounds. *(Not met — A1.)*
- **FR-028**: After the one documented authoring step, running processes MUST serve the new or edited skill; a skill-tag naming no product, a
  specialist tool that does not exist and a `model` alias the proxy does not serve MUST be caught before they ship. *(Not met — A3, A4, A7.)*

### Key Entities *(include if feature involves data)*

- **Skill**: A named instruction bundle on disk — name, description, body, optional product tags. Authoritative on disk; mirrored (name and
  description only) in a search index.
- **Skills Index**: A dedicated vector collection of `{name, description}` entries rebuilt by an explicit command.
- **Subagent**: A named specialist on disk — name, description, system prompt, optional tool list, model alias and product tags.
- **Subagent Registry**: Per product: each declared specialist paired with its resolved, read-only-only tool set.
- **Delegated Run**: One nested, isolated, budgeted execution with its own thread id; produces an answer and a token/cost pair.
- **Subagent Spend**: The per-turn list of `(tokens, cost)` pairs a turn's delegations reported, reset each turn.
- **Compiled Nested Graph**: The reusable executable for one (product, specialist), including its in-process checkpoint store.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a task matching a bundled procedure, the assistant loads that procedure (and no other) before acting — verified live on the
  small local model; a turn needing no procedure loads none.
- **SC-002**: A procedure or specialist is never searchable, loadable or delegable in a product it was not declared for: 0 leaks across the
  three example products and the general product. *(Not met — B15: two shipped skills reach every product.)*
- **SC-003**: Across every product, 0 tools reachable from a specialist are anything but read-only, and the delegation depth is exactly 1.
- **SC-004**: A delegation never triggers an approval pause (0 pauses across all products and with the approval flag on or off).
- **SC-005**: Every delegated run ends within its step, token, cost and time ceilings; no run's outcome is silent (one counter increment per run).
- **SC-006**: After N completed delegated runs the process retains no per-run conversation state. *(Not met — B13: the retained thread count equals
  N; 100 runs → 100.)*
- **SC-007**: 100% of tokens a delegated run spends reach the tenant ledger, including runs that time out or error. *(Not met — B14: a 500-token run
  that timed out recorded none.)*
- **SC-008**: One malformed, duplicate or unreadable catalog file never removes any other entry (2 skipped files → N−2 entries unaffected).
- **SC-009**: A fresh container start serves the same non-empty catalogs as a host-native start. *(Not met — B16: 0 skills, 0 subagents,
  no delegation tool in the built image.)*
- **SC-010**: A typo in a product tag, tool name or model alias in a shipped file fails a test before release. *(Not met — A4, A7.)*

## Assumptions & Known Gaps

**Assumptions**

- Skills and specialists are *bundled, repository-authored* content, reviewed like code; they are trusted as instructions. (A tenant-authored
  catalog is a different feature with a different trust model.)
- The nested run is bounded to finish inside one tool call; there is deliberately no way to pause and resume it, which is why it is read-only.
- A small local model is the reference model; several defaults (top-k of 1, list order) were tuned live against it and are not arbitrary.
- An operator who adds or edits a catalog file knows to run `make index-skills` and restart services; nothing automates either today (A2, A3).

**Out of scope for this feature**

- The graph, approval gate, mandatory read-only/mutating/outward declaration and budget machinery (features 001, 003).
- Which tools each product exposes (feature 005); the ledger, tenant budgets and dashboards (feature 008); sandboxes and MCP (feature 009).
- Tenant-authored skills, writable subagents, a subagent that can pause for approval, nested delegation, skill versioning.

**Known gaps (disclosed, with how each was established)**

- **Bug B13 — the compiled nested graph keeps every delegated run's whole conversation forever (reproduced at function level).** The graph is
  cached per (product, specialist) and compiled with an in-process checkpoint store; each run uses a **fresh, unique** thread id, so nothing is
  ever overwritten and nothing is ever deleted. Reproduced with 100 delegations through the real implementation and the cache on: the store held
  1, 10, 50 and 100 threads after 1, 10, 50 and 100 runs. A long-lived worker therefore grows without bound in proportion to delegations ×
  conversation size; pattern 46 and the code comments call the store "ephemeral"/"throwaway", which stopped being true when the graph cache was
  added. The installed checkpoint store has a `delete_thread` (verified), so the fix is small. Not fixed here.
- **Bug B14 — tokens spent by a delegated run that times out or errors are never recorded (reproduced at function level).** The ledger write sits
  *after* the awaited run, so a timeout (`TimeoutError`) or any exception re-raises before it; the parent's running total is not updated either,
  because the tool raises instead of returning the spend. Reproduced with a model that returned a 500-token step and then stalled past a shortened
  timeout: the call raised `TimeoutError` and **zero** usage rows were recorded. A specialist that routinely times out therefore costs tokens that
  tenant budgets and dashboards never see. Not fixed here; the partial state is recoverable from the run's checkpoint before it is deleted (to
  be verified by the failing test).
- **Bug B15 — two shipped skills are visible in every product, though three of the four lack the tools they name (reproduced against the real
  catalog).** `onboarding-brief` (instructs `query_employees`) and `expense-summary` (instructs `calculator`) have no `domains` tag, so — by the
  deliberate "no tag means every product" default — they are offered in support, ops and sales, none of which exposes `query_employees` or
  `calculator` (checked against each product's resolved tool list). Reproduced: with the real catalog, support, ops and sales each list both, and
  `use_skill("onboarding-brief")` in the support product returns the full instructions. This is exactly the leak the constitution's *Composition*
  constraint says tags exist to prevent. The mechanism is right; the two files were never tagged `[ecorp]`. Not fixed here.
- **Bug B16 — the container images contain no skills and no subagents (established by inspecting the built image and the build files).** The
  Dockerfile copies only `app/`; neither compose file mounts `skills/` or `subagents/`; `.env.example` sets no directory override. Inspecting the
  locally built API image (built after the Dockerfile's last change): no `/app/skills`, `/app/subagents` or `/app/scripts`; `get_skills()` and
  `get_subagents()` both return 0 entries; and `run_subagent` is **absent** from the tool list (its construction is guarded by a non-empty
  registry). The release workflow builds the same Dockerfile for the production image (read; not run). Everything in this feature — and the README's
  onboarding-brief and `researcher` examples — therefore works only on a host-native start. Nothing notices: the live tier starts the API as a subprocess
  from the repository root (where the folders exist) and runs the indexing script by module, and CI's container job only builds the image. Not fixed here.
- **A1 — the assistant-supplied `query`, `name` and `task` are unbounded.** `skill_search.query` and `use_skill.name` have no length limit, and
  `run_subagent.task` only rejects blank. Principle III requires bounds on model-supplied strings; the note and memory tools have them. `task`
  becomes the nested run's sole human message, so its size is only limited indirectly by the parent's own context. *(Read.)*
- **A2 — nothing builds the skills index in any deployment path, and a missing index degrades silently.** `make index-skills` exists; the only
  automated caller is the host-native `restart-all` target. No compose service, entrypoint or CI step runs it (and `scripts/` is not in the image — see
  B16). `skill_search` on a missing index returns its "has `make index-skills` been run?" message and logs a warning, but increments no metric
  (Principle V requires one for every degrade-and-continue path). The command also recreates the collection (delete then create), so a search running
  during a re-index briefly finds no catalog. *(Read.)*
- **A3 — a running process does not see an added or edited skill after re-indexing.** `get_skills()` caches the disk catalog for the life of the
  process; the only caller of `reload_skills()` is the indexing script, in its own process. After a re-index the index lists the new name but the
  server's cache does not, so the hit is dropped as "no longer on disk"; an edited body is served stale until restart. Specialists have the stricter
  restart requirement already disclosed. *(Read: callers of the reload hooks.)*
- **A4 — nothing validates that a skill or specialist's tags, tool names and model alias refer to anything.** The loaders are deliberately
  domain-agnostic, so a `domains: [suport]` typo silently hides a skill from everyone; an unknown tool in a specialist's list is dropped with a log
  line only (no metric); an unknown `model` alias is accepted and fails at the first call as an `error` outcome. The proxy's aliases today are
  `chat`, `chat-backup`, `vision`, `embed`. *(Read.)*
- **A5 — required-tool detection is a crude substring check and keeps a dead branch.** `_SKILL_REQUIRED_TOOL_MARKERS` is an allowlist matched as a
  substring of the skill text (the code says so), so a skill that merely *warns against* a listed tool would trigger it. Separately, `use_skill`
  appends a "call `run_command_in_sandbox`" reminder when the body names that tool, but no shipped skill does (the shipped ones name
  `run_python_in_sandbox`), and no test covers that branch; two independent lists of "tools a skill may require" exist. *(Read; shipped skills
  grepped.)*
- **A6 — a specialist's answer re-enters the parent as plain tool text.** It is credential-scrubbed but not framed as data; it is model-written text
  that may paraphrase untrusted retrieved content. This matches how other read-only tool output is treated and the blast radius is bounded by
  Principle II (every mutating parent tool still pauses), so it is a design note, not a regression — recorded so the choice is explicit. *(Read.)*
- **A7 — no test pins the shipped catalogs.** The real specialists' per-product menus are asserted in each product's domain tests, but no test loads
  the real `skills/` folder or checks that tags name real products, that a skill's instructed tools exist where it is visible, or that the catalogs
  are in the image — which is how B15 and B16 shipped. *(Read: no test calls the real loaders.)*
- **A8 — configuration and comment drift.** `SKILLS_DIR`, `SKILLS_COLLECTION`, `SKILLS_SEARCH_TOP_K`, `SUBAGENTS_DIR`, `SUBAGENT_TIMEOUT_SECONDS` and
  `MAX_SUBAGENT_COST_USD_PER_RUN` are settings but have no `.env.example` entry (the repo rule is that every tunable does). The comment on
  `max_subagent_cost_usd_per_run` still says a subagent's spend is *not* folded into the parent turn's total, which was fixed (the reducer and its
  tests exist); several comments cite `tools.py::run_subagent`, which now lives in `subagent_tools.py`; and "Extending Further" in
  `GRAPH_PATTERNS.md` lists none of B13–B16. *(Read.)*
