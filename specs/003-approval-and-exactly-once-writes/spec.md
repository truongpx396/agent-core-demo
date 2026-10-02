# Feature Specification: Mandatory Approval and Exactly-Once Writes

**Feature Branch**: `003-approval-and-exactly-once-writes`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — with two reproduced defects, see *Known gaps* B3 and B4

**Input**: User description: "Mandatory human approval and exactly-once side effects (retrospective spec of the as-built system): every tool declares whether it only reads, mutates, or reaches outward; any non-read-only action pauses for an explicit human decision that no setting can bypass; a pause is durable, resumable and cancellable; unattended callers decline rather than approve; and every write is safe to run twice for the same logical action — through call-id idempotency, row-level uniqueness at the target, deterministic ids, verify-don't-retry on timeouts, and crash recovery that continues rather than restarts a turn."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01, commit
> `6089718`) from the code, the constitution (Principles II and IV are NON-NEGOTIABLE) and
> `GRAPH_PATTERNS.md` patterns 8, 15, 36, 43 and the "Extending Further" duplicate-side-effect
> rounds. It describes what the system does today. Two defects were found by *reproducing* behavior
> with a hermetic harness while writing it (B3, B4); a third gap (no tool-level test that every
> write tool is wrapped, A7) was found by auditing every declared write tool. All are in *Known
> gaps*. Companion specs: `001-core-rag-agent-turn` (the pipeline whose routing sends calls to the
> gate) and `002-tenant-isolation-and-memory` (identity and ownership; B2 there interacts with B3
> and the approval authority here).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Nothing with a side effect happens without a person's explicit yes (Priority: P1)

The assistant can *propose* to create a ticket, log an incident, send a message, save a note or a
memory, or reach out to an outside system — but it can never *do* any of those on its own. Every
such action pauses the conversation and asks a human to approve or reject it, showing which action
and with what arguments. There is no setting, flag or per-domain option that turns this off. An
action the system does not recognise is treated as the most dangerous kind and is gated too.

**Why this priority**: The assistant reads untrusted text on almost every turn. Write capability plus
untrusted content is one gamble away from a document steering a real write. A mandatory human gate
is the control that makes giving the assistant write tools acceptable at all.

**Independent Test**: Make the assistant request a read-only action, a mutating action and an
unregistered action. Confirm only the read-only one runs immediately, the other two pause and name
themselves, and nothing is written until a person approves.

**Acceptance Scenarios**:

1. **Given** the assistant requests a tool declared as reading only, **When** the request is
   routed, **Then** it runs without a pause.
2. **Given** it requests a tool declared as mutating or as reaching outward, **When** it is
   routed, **Then** the conversation pauses and the person is shown each pending action's name and
   arguments; nothing has run.
3. **Given** a tool that was never declared at all, **When** it is requested, **Then** it is
   treated as reaching outward and gated.
4. **Given** the caller's "approve everything" option is off, **When** a write is requested,
   **Then** it still pauses (the option can only *add* pauses for read-only actions, never remove
   the mandatory one).
5. **Given** the person approves, **When** the decision arrives, **Then** every pending action
   runs.
6. **Given** the person rejects, **When** the decision arrives, **Then** none run, the assistant is
   told each was rejected, and it may answer or propose something else.
7. **Given** the assistant names a tool that does not exist, or requests too many actions at once,
   **When** the request is inspected, **Then** it is bounced back to the assistant without ever
   reaching a human (a human cannot meaningfully approve a name that does not exist).

---

### User Story 2 - A write happens at most once for a logical action (Priority: P1)

Whether a call is replayed by a retry, a crash-recovery sweep, a double-submit or a repeated
approval, the real side effect — a ticket, an incident, a note, a message — happens once. This is
enforced in two independent layers: a record that a specific call already ran (so a replay returns
the first result), and a uniqueness rule at the place the data lands (so even if the first layer is
bypassed the data is not duplicated). A write that times out is *not* retried blindly: the
assistant is told it may already have been applied and is steered to check first.

**Why this priority**: This system needed repeated audit rounds to close duplicate-write windows;
each round found a gap the previous keying could not see. Duplicates in a ticket queue or an outbound
message are customer-visible.

**Independent Test**: Run the same write call twice with the same call id (and then simulate the
first run's bookkeeping being lost). Confirm one row/point/message results. Force a write to time out
and confirm the assistant is told to verify, not to retry.

**Acceptance Scenarios**:

1. **Given** a write call that has already completed, **When** the same call (same id) arrives
   again, **Then** the first call's result is returned and the side effect does not run again.
2. **Given** the first layer's bookkeeping is unavailable, **When** a write runs, **Then** it still
   runs (the layer fails open), the degradation is counted, and an operator alert fires if it
   persists.
3. **Given** two runs for the same call id both reach the storage step, **When** the second writes,
   **Then** the target's uniqueness rule makes it a no-op and it returns the first row's identity.
4. **Given** an "append" (a comment on a ticket, a note on a lead), **When** it is replayed,
   **Then** it is a separate keyed row, not text concatenated onto a shared field, so a replay is a
   detectable duplicate instead of silently doubled text.
5. **Given** a vector-store write (a note or a memory), **When** it is replayed, **Then** its
   record identity is derived from the call id so the replay overwrites the same record.
6. **Given** a write whose own time limit expires, **When** the failure is reported, **Then** the
   message tells the assistant the effect may already have happened, not to call it again blindly, and
   to check with a read-only tool; no automatic retry is made.
7. **Given** two *different* call ids for what is really the same request, **When** both run,
   **Then** both take effect — the system keys on the call, not on a business meaning (a deliberate,
   disclosed limit).

---

### User Story 3 - A paused conversation is durable and can be approved, rejected or cancelled (Priority: P1)

A pause is a real state, not a transient prompt. It survives a restart or a redeploy and can be
resumed by any worker. A person can approve, reject, or *cancel*; cancel is a third, distinct outcome
that ends the run outright without giving the assistant a chance to react. A new message sent while
a conversation is paused is refused with the pending actions shown — never silently discarded and
never auto-cancelled. A conversation reopened from a session list shows its pending approval again.

**Why this priority**: An approval gate that disappears on restart, or that a stray message can
silently clear, is not a control. Cancellation must be unconditional to be useful.

**Independent Test**: Pause on a write; restart the processes; resume with approve → the action runs
once. Pause again; send a new message → refused with the pending actions listed. Pause again; cancel →
the run ends and the model produces nothing further.

**Acceptance Scenarios**:

1. **Given** a conversation paused at an approval, **When** all processes restart, **Then** the pause
   is still there and a resume completes the turn.
2. **Given** a resume request for a conversation that is not actually paused (completed, never
   existed, or merely still running), **When** it arrives, **Then** it is refused as "no paused run"
   and counted — a mid-run conversation is *not* treated as paused.
3. **Given** a conversation paused under an incompatible older build, **When** a resume arrives,
   **Then** it is refused as "incompatible" and counted; a mere difference in build identity is not
   incompatibility.
4. **Given** a conversation paused and resumable, **When** a *new* message arrives, **Then** it is
   refused with a "pending approval" error that lists the pending actions.
5. **Given** a conversation paused but *not* resumable (incompatible), **When** a new message
   arrives, **Then** it proceeds, with a visible note that the earlier approval could not be resumed.
6. **Given** a paused conversation, **When** the person cancels, **Then** the gated action never
   runs, the run ends, and the assistant is not given another turn.
7. **Given** a turn that is actively streaming (not paused), **When** the person cancels, **Then** it
   stops at the next event boundary with a "cancelled" error; a tool already mid-flight is not
   interrupted.
8. **Given** the person returns to a paused conversation from a session list, **When** it is
   opened, **Then** the pending approval and whether it is resumable are shown again.
9. **Given** a resume request, **When** it is made, **Then** the resumer's identity is supplied again
   with the request — identity is not remembered from the original pause.

---

### User Story 4 - A crashed worker never duplicates a write (Priority: P2)

Jobs are delivered at least once: a job is acknowledged only after it finishes, so a worker that dies
mid-job leaves it to be reclaimed. Reclaiming never blindly re-runs a turn. A turn that had already
started is *continued* from its saved state, so work that already completed is not repeated and no
new call ids are minted; a turn that already finished or is paused is not retried at all; a resume
or a cancel is always safe to repeat. Only one job at a time may run against a conversation. Retries
are capped, and a job that cannot be safely retried is reported to its caller as a lost worker and
archived for inspection.

**Why this priority**: A crash is the most likely way a write gets repeated. Restarting a crashed
turn re-asks the model, which mints brand-new call ids that no id-keyed defense can recognize.

**Independent Test**: Kill a worker after a write completed but before the job was acknowledged;
confirm the reclaimed job is continued, the write is not repeated, and the caller gets a result or a
lost-worker error — never a second write.

**Acceptance Scenarios**:

1. **Given** a job whose turn has started but not finished, **When** it is reclaimed, **Then** it is
   continued from its saved state, not restarted from the question.
2. **Given** a reclaimed job whose turn already produced its final answer, or is paused, **When** it
   is classified, **Then** it is not retried (archived and the caller told the worker was lost).
3. **Given** a reclaimed resume or cancel job, **When** it is retried, **Then** that is always safe
   (a resumed call that already ran just returns its first result).
4. **Given** the retry cap is reached, or the job is unsafe to retry, **When** it is reclaimed again,
   **Then** the caller receives a lost-worker error and the job is archived to a dead-letter store.
5. **Given** a second job arrives for a conversation that already has one running, **When** it is
   dispatched, **Then** it is rejected immediately as busy rather than queued behind or run alongside.
6. **Given** a client retries an identical submission within a short window, **When** it arrives,
   **Then** it shares the first attempt's turn and stream instead of starting a second turn.
7. **Given** a job's handler fails, **When** it is processed, **Then** the job is still acknowledged
   (it is not redelivered to repeat a side-effecting call) and the caller receives an error.

---

### User Story 5 - Unattended callers and background jobs never write unreviewed (Priority: P2)

Some callers have nobody to ask — a chat-app bot, a fire-and-forget job. They automatically
**decline** a pause (never approve), count it, and tell the user the action was not approved. Scheduled
jobs that genuinely need to write do not enter the assistant's tool loop at all: they call the
domain's fixed write functions directly as a fixed pipeline. Delegated sub-assistants can only use
read-only tools and cannot delegate further.

**Why this priority**: The mandatory gate has no unattended bypass by design; the alternatives for a
caller with no human must be "decline" or "don't use the loop", never "approve".

**Independent Test**: Drive a chat-app turn that proposes a write → no write, a decline is counted, the
user gets a reply. Inspect the catalog of a delegated sub-assistant → no mutating/outward tool, no
delegation tool.

**Acceptance Scenarios**:

1. **Given** an unattended turn pauses once, **When** the pause is detected, **Then** it is declined,
   `unattended pause` is counted, and the conversation continues.
2. **Given** a delegated sub-assistant is declared with a mutating or unknown tool, **When** its
   catalog is built, **Then** that tool is dropped with a warning — never upgraded — and the
   delegation tool is excluded from every sub-assistant's catalog.
3. **Given** a scheduled job needs a write, **When** it runs, **Then** it calls the domain's write
   function directly and never the agent loop.
4. **Given** an unattended turn pauses a *second* time after the first decline, **When** the model
   re-requests the action, **Then** *(intended)* the conversation is not left stranded. **As built it
   is — see B4.**

---

### User Story 6 - Adding a write tool is a checklist, and the checklist is enforced (Priority: P3)

A developer adding a tool that mutates or reaches outward follows a fixed checklist: declare its
tier; check identity first; route the call through the exactly-once wrapper; make the target write
idempotent (a unique call-id column with do-nothing-on-conflict, an append as its own row, a
deterministic id, or a naturally idempotent update); scope its queries by tenant; and add tests. A
degraded exactly-once store, a failed outbound notification and a failed upload each have an
operator alert.

**Why this priority**: The two NON-NEGOTIABLE principles stay true only if every future tool is added
correctly. It ranks last because it concerns future change, not today's behavior.

**Independent Test**: Audit every tool declared as mutating or outward: each declares its tier and goes
through the exactly-once wrapper (all 15 statically declared tools, plus the four sandbox tools in
each of the three domains that expose them, at audit time). For a new tool, the checklist's tests fail
if the wrapper or identity check is omitted *(not yet enforced — see A7)*.

**Acceptance Scenarios**:

1. **Given** the default tool set, **When** it is inspected, **Then** every tool has a declared tier.
2. **Given** a mutating or outward tool, **When** its body is read, **Then** it checks identity, then
   calls the wrapper with its call id, then performs a time-limited write.
3. **Given** the exactly-once store is unreachable, **When** a write runs, **Then**
   a degradation counter increments and, if sustained, an operator alert fires.

---

### Edge Cases

- A tool whose own time limit expires may already have committed on the far side; the time limit
  cancels the *waiting*, not necessarily the underlying write.
- A rejection or cancellation must produce one response message per pending action, or the next
  model call is malformed.
- The result of a replayed write is the *first* call's stored text, which can be stale relative to the
  current state of the target.
- Two replays racing while the first is still running can both pass the first layer (the second sees
  an incomplete record) — the target-level uniqueness rule, not the first layer, is what prevents the
  duplicate for pure inserts, appends and vector points.
- A timeout in a *read-only* listing may be retried automatically; a write is never retried
  automatically.
- A conversation approved after being cancelled earlier — see B3.
- An unattended channel whose model re-requests a declined write — see B4.
- A resume arriving while the original worker is still streaming the turn is refused as "no paused
  run" rather than starting a competing execution.

## Requirements *(mandatory)*

### Functional Requirements

**Capability declaration and the mandatory gate**

- **FR-001**: Every tool MUST declare exactly one tier — read-only, mutating, or outward — and a tool
  missing from its domain's declaration MUST be treated as outward.
- **FR-002**: Any pending action whose tier is not read-only MUST route to the approval gate
  unconditionally. No flag, environment variable, caller option or per-domain setting may bypass or
  skip it. An opt-in "approve everything" option MAY add pauses for read-only actions.
- **FR-003**: The approval request MUST present, for each pending action, its name and arguments.
- **FR-004**: An approved decision MUST run every pending action in the batch; a rejected decision MUST
  run none and MUST give the assistant exactly one rejection message per pending action; a cancelled
  decision MUST run none, give one cancellation message per pending action, and end the run without
  returning control to the assistant.
- **FR-005**: A batch containing a non-existent tool name, an over-large batch, or a skill-load without
  a prior skill search MUST be returned to the assistant *before* the gate and MUST NOT reach a human.
- **FR-006**: Each decision MUST be counted by outcome (approved / rejected / cancelled), and each
  mandatory gating MUST be counted by tier.

**Durable pause, resume, cancel**

- **FR-007**: A pause MUST persist across restarts and be resumable by any worker; "paused" MUST be
  defined as having a pending interrupt, not merely having work remaining.
- **FR-008**: A resume MUST be refused, with a counted reason, unless the conversation is paused and
  its saved-state schema version equals the running build's; a difference in build identity alone MUST
  NOT be refusal.
- **FR-009**: A resume request MUST re-supply the resumer's identity; identity MUST NOT be taken from
  the original pause. *(Who may resume is feature 002's ownership gap, B2.)*
- **FR-010**: A new message to a conversation paused and resumable MUST be refused with a
  "pending approval" error listing the pending actions, and MUST NOT auto-cancel or discard them. If the
  pause is not resumable the message MUST proceed with a visible system note.
- **FR-011**: Cancel MUST work in both states: for an actively streaming turn, by a cooperative flag
  checked between events (so it takes effect at the next event boundary); for a paused turn, by a
  distinct third decision. Cancel MUST never be rate-limited. **As built, the streaming case is not
  honored for a turn that is streaming after an approval — see A12.**
- **FR-012**: A person returning to a conversation MUST be able to retrieve its pending approval and
  whether it is resumable.

**Unattended callers and delegated assistants**

- **FR-013**: A caller with no human MUST auto-decline a pause, MUST NOT auto-approve, and MUST count it.
- **FR-014**: A scheduled or unattended job that needs a write MUST call the domain's write
  functions directly as a fixed pipeline and MUST NOT enter the tool-calling loop.
- **FR-015**: A delegated sub-assistant's catalog MUST contain only read-only tools; any declared tool
  that is missing, undeclared, or not read-only MUST be dropped with a warning and never upgraded; the
  delegation tool MUST NOT appear in a sub-assistant's catalog.

**Exactly-once**

- **FR-016**: Every mutating or outward tool MUST, in order: check identity (refusing if absent), call
  the exactly-once wrapper with the model-provider's per-call id, and perform a time-limited write.
- **FR-017**: The wrapper MUST run the write at most once per call id: the first caller claims the id
  atomically; a later caller receives the first result unchanged. It MUST fail *open* on its own
  storage failure (run the write unprotected, count it, log it) because it is a defense in depth, not a
  precondition for a tool to work.
- **FR-018**: The write itself MUST also be idempotent at the target: pure inserts carry a nullable
  unique call-id column with do-nothing-on-conflict and read the existing row back on a conflict; an
  append MUST be its own row keyed by call id, aggregated at read time; vector-store records MUST derive
  their id from the call id or content, never a random draw; an update MUST be naturally idempotent or
  document why not.
- **FR-019**: A write's own time-limit expiry MUST surface as a distinguishable "timed out, may already
  have happened" failure that tells the assistant to verify with a read-only tool and not to retry
  blindly; the system MUST NOT automatically retry a timed-out write.
- **FR-020**: Automatic retry MUST be limited to exceptions the caller can prove mean "the call never
  landed", and MUST NOT wrap a write or any non-idempotent outward call; a bare catch-all retry is
  forbidden.
- **FR-021**: A best-effort outbound notification backing an already-committed write MUST never raise,
  MUST be counted by outcome, and MUST have an operator alert.
- **FR-022**: Stored exactly-once records MUST be removable by a retention sweep so they do not
  accumulate forever.

**Delivery and crash recovery**

- **FR-023**: A job MUST be acknowledged only after its handler finishes (or fails), so a crashed
  worker leaves it reclaimable; a failed handler MUST still acknowledge it and report an error to the
  caller rather than be redelivered.
- **FR-024**: Reclaiming MUST classify a turn from its saved state without re-running anything: continue
  a started-but-unfinished turn from its saved run; never retry a finished or paused turn; always allow
  a resume or cancel retry; start fresh only if nothing was ever saved.
- **FR-025**: Reclaim retries MUST be capped; beyond the cap, or when unsafe, the caller MUST receive a
  lost-worker error and the job MUST be archived to a bounded dead-letter store, counted by outcome.
- **FR-026**: At most one job MAY run per conversation at a time, across all workers; a competing job
  MUST be rejected immediately as busy; the lock MUST be released before the terminal event is
  published and MUST expire on its own for a crashed holder.
- **FR-027**: An identical resubmission within a short window MUST reuse the first attempt's request and
  stream; if publishing fails after the claim wins, the claim MUST be released.
- **FR-028**: Waiting for a counterparty MUST have a deadline (a stream with no first event within its
  deadline ends with an error), so a missing worker never hangs a caller.

**Process**

- **FR-029**: A feature that adds a mutating or outward tool MUST state its tier, tenant scoping and
  duplicate-write story up front; adding such a tool MUST follow the checklist in
  `.claude/rules/side-effect-tools.md`.

### Key Entities *(include if feature involves data)*

- **Tool Capability**: One of read-only, mutating, outward, declared per tool; a missing declaration
  means outward.
- **Pending Action**: A tool call the assistant has proposed (name, arguments, call id) that has not run.
- **Approval Request / Decision**: The pause that lists the pending actions, and the person's answer:
  approve, reject, or cancel.
- **Exactly-Once Record**: One row per call id — the call id (unique), tenant, conversation id, tool name,
  the stored result (empty while in flight), a creation time.
- **Target Row Key**: A nullable unique call-id column on each pure-insert or append table, or a record id
  derived from the call id or content.
- **Job / Delivery**: A queued unit of work for one conversation (new turn, continued turn, resume, or
  cancel), delivered at least once and acknowledged on completion.
- **Dead Letter**: An archived job that could not be safely retried, kept for inspection.
- **Cancel Flag / Conversation Lock**: Short-lived markers keyed by conversation that stop a streaming
  turn and exclude a second concurrent job.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of requested actions declared mutating or outward — and of actions with no declaration —
  pause for a human decision; zero such actions run before one, under every setting.
- **SC-002**: Replaying a write call any number of times with the same call id produces exactly one
  stored result for every tool whose target has a uniqueness rule (all inserts, appends and
  vector-store writes) and one unchanged end state for tools that only update a record; and, for the
  tools whose effect has no addressable target — those that send a message (the escalation and handoff
  tools, which also update a record, and the team-channel tool) or call an outside system or sandbox —
  exactly one under the system's single-claimant guarantee only (see the residual-window gap, R1).
- **SC-003**: A paused conversation is still paused, and resumes with its decision applied, after every
  process is restarted.
- **SC-004**: 100% of messages sent to a paused, resumable conversation receive the pending-approval
  refusal; none is silently dropped and none auto-cancels the pending action.
- **SC-005**: After a cancel of a paused conversation, or of a new or continued streaming turn, the
  assistant produces no further output for that run and the gated action does not run. **Not met for a turn
  streaming after an approval (A12).**
- **SC-006**: A worker crash after a write completed never yields a second write: the turn is continued,
  or the job is archived with a lost-worker error — never restarted from the question.
- **SC-007**: Unattended callers perform zero writes; every auto-decline is counted.
- **SC-008**: A degraded exactly-once store is visible to an operator: an alert fires once the
  degradation has persisted for 15 minutes.
- **SC-009**: Every tool declared mutating or outward is routed through the exactly-once wrapper (all 15
  statically declared tools and the four sandbox tools in each of the three domains, at audit). *Not
  yet enforced by a test (A7).*
- **SC-010**: A conversation that was cancelled once still accepts, and runs, an approved action on a
  later turn. **Currently not met — see B3.**
- **SC-011**: An unattended conversation is never left in a state where the user can neither continue nor
  recover it. **Currently not met on the chat-app channel — see B4.**

## Assumptions & Known Gaps

**Assumptions**

- The model provider (or its proxy) assigns a distinct call id to every tool call it makes, and that id
  is unique across tenants; two invocations sharing an id are the same logical invocation.
- At most one turn is active per conversation (enforced by the conversation lock), which is what makes the
  first layer's narrow race acceptable for inserts.
- A person approving an action is an authorized human; the system records the *decision* but not who made
  it (see below).
- Time limits cancel the waiting task, not necessarily the underlying write.

**Out of scope for this feature**

- Isolation, identity and conversation ownership → `002-tenant-isolation-and-memory`.
- The turn pipeline, budgets and streaming → `001-core-rag-agent-turn`.
- Domain-specific tools beyond their tier and wrapper (ticket/CRM/ops semantics), the per-tenant
  budget, and the ingestion pipeline's own idempotency — not covered by this spec batch.

**Known gaps (disclosed, with how each was established)**

- **Bug B3 — a cancelled conversation cannot approve a later action (reproduced).** The "cancelled"
  marker is set when a paused action is cancelled and is never cleared, including at the start of the
  next turn. Reproduced with a fake model and an in-memory store: after one cancelled approval, a later
  turn on the same conversation paused, the person **approved**, and the run ended immediately — no
  action result was produced, the last message was the assistant's own tool request with empty content,
  and the run reported finished. The approved action silently did not run. By reading, the dangling tool
  request then sits in the conversation's history, which a real model provider would reject on the next
  call; that consequence was **not** reproduced. The marker is checked before the approval flag, so it
  wins. The existing tests cover the cancel itself and the routing function in isolation, not a later
  approval on the same conversation. Not fixed here.
- **Bug B4 — an unattended conversation can be stranded (reproduced at function level).** The unattended
  path declines *one* pause. If the model then re-requests the same gated action, the conversation
  pauses a second time and stays paused. The chat-app channel ignores that second pause, so (a) the
  user receives **no reply at all** to their first message (an empty reply sends nothing), contradicting
  the channel's documented promise of "a real reply explaining it wasn't approved", and (b) every later
  message is refused with "pending approval — approve, reject, or cancel it", which that channel offers
  no way to do — so the conversation is stuck. The precondition (the model re-requesting after a decline)
  is model behavior; the stuck state is system behavior. The helper's own documentation says a second
  pause is "left to the model's own next response", which does not match what the code does. Reproduced
  with a scripted model that requests the write twice; not reproduced against a real model or a real
  chat-app connection.
- **A7 — nothing pins that every write tool is wrapped.** An audit of all 15 statically declared mutating or
  outward tools (and the four sandbox tools in each of the three domains) found each routes through the
  wrapper, but no test fails if an edit drops it: tests cover the
  wrapper itself and the *default* tool set's declarations; the domain write tools have no tool-level
  tests of "refuses without identity" or "goes through the wrapper" (the checklist asks for them).
- **E2 — the exactly-once lookup is not tenant-scoped.** A call id already seen returns the stored result
  regardless of which tenant stored it (the table's tenant column is metadata). Safe only under the
  uniqueness assumption above; a collision would return another tenant's result text. Cross-reference
  feature 002.
- **The refusal of a new message to a paused conversation has no test (found by `/speckit-analyze`).**
  FR-010 — never auto-cancel, list the pending actions — is a MUST that nothing exercises: the paused-state
  check, the "not resumable" note path, the same refusal on a continued turn, and the identity the resumed
  action runs under are all untested (task T050). The behavior was read from the code, not run.
- **A13 — the crash-recovery sweep is not tested against a real Redis (by reading and grepping the tests).** The
  reclaim of an abandoned job relies on the broker's own "claim entries idle longer than N" operation and its
  pagination. The only tests of it use a hand-written stand-in that its own documentation says implements "just
  enough" and is not paginated like the real one; no integration test touches it. Delivery to exactly one worker
  and the per-conversation lock *are* tested against a real broker. Not reproduced against a real broker.
- **The project's own documentation is out of date on crash recovery (A11).** `GRAPH_PATTERNS.md` and two
  code comments still describe a safety check that was removed; they say a turn that already ran a write
  is refused, whereas the system now *continues* such a turn from its saved state (FR-024).
- **R1 — a residual duplicate window remains for effects without a target row.** If a second run sees the
  first's record still in flight (empty result), it runs the write itself. Inserts, appends and
  vector-store writes are protected by the target-level rule; an outward action such as sending a
  message or calling an outside system is protected only by the conversation lock and by reclaim
  waiting longer than a turn can run.
- **No business-key uniqueness.** Two different call ids for what is semantically one request both take
  effect (deliberate: what counts as "the same ticket" is a product decision).
- **A10 — approvals are not attributed.** A decision is counted by outcome only; who approved, when, and for
  which action is not recorded. Combined with feature 002's B2, any caller who can name a conversation
  can resolve its pending approval, and an approved action runs under the *resumer's* identity.
- **A9 — exactly-once records are only removed by a manually run sweep** (default 24 h retention); nothing
  schedules it.
- **A failed job is acknowledged**, so it is not repeated — safe against duplicate writes, but the caller
  must resubmit; the job is not archived (only unsafe-to-retry *reclaimed* jobs are).
- **Cancellation of a streaming turn is cooperative**: it takes effect at the next event boundary and does
  not interrupt a tool already running.
- **A turn that is streaming *after an approval* cannot be cancelled (A12, by reading the code).** The
  cooperative cancel check is wired only into new-turn and continued-turn jobs; the resume job and the
  resume entry point take no cancel check. A cancel request during the post-approval run finds no paused
  run (the cancel job reports "nothing to cancel") and the flag it sets is never read, so the approved
  action and any follow-on model rounds run to completion or the turn timeout. Not reproduced against a
  running stack.
- The one-shot operations-investigation script has no resume loop, so a gated action ends with an empty
  answer and nothing written (disclosed in the script itself).
