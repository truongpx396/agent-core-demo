# Feature Specification: Core RAG Agent Turn Pipeline

**Feature Branch**: `001-core-rag-agent-turn`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — reconciled 2026-10-03 against the fixes merged since (#62, #64, #68, #69); see *Resolved since this spec was written*

**Input**: User description: "Core RAG agent turn pipeline (retrospective spec of the as-built system): a user asks a question in a conversation and receives a streamed, source-cited answer from a local, tool-using assistant. Every turn is bounded (rounds, tool fan-out, tokens, cost, wall-clock, repeated actions), screened before any spend, quality-gated before delivery, and resumable across restarts; long conversations are compacted rather than growing without limit."

> **Retrospective note.** Spec Kit was adopted after this system was built (commit `6089718`,
> 2026-10-01). This spec was reverse-engineered from the shipped behavior, the constitution
> (`.specify/memory/constitution.md`) and `GRAPH_PATTERNS.md`; it describes what the system does
> today, not a proposal. Where the shipped behavior has a known gap, the gap is stated in
> *Assumptions & Known Gaps* instead of being papered over (Constitution Principle VIII).
> Identity and tenant isolation are specified in `002-tenant-isolation-and-memory`; the approval
> gate and exactly-once side effects in `003-approval-and-exactly-once-writes`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ask a question, get a streamed answer with real sources (Priority: P1)

A person types a question into a conversation. The assistant looks up relevant material in the
organization's knowledge base before reasoning, may call fixed tools (look up documents, do
arithmetic, query a staff directory, ask a clarifying question), and streams its answer back as
it is produced. Every statement that rests on retrieved material ends with a numbered marker
that points at a real source; at the end of the answer the person receives the list of sources
actually used and, when the answer was grounded, two or three suggested follow-up questions.

**Why this priority**: This is the product. Every other story exists to make this one safe,
bounded, and trustworthy. Delivering only this story is already a usable (if unguarded) assistant.

**Independent Test**: Send one factual question that the knowledge base can answer. Confirm the
answer arrives incrementally, ends with exactly one terminal outcome, every `[n]` marker in the
final text matches a returned source, and the returned source list contains only sources the
answer actually cited.

**Acceptance Scenarios**:

1. **Given** a knowledge base containing a relevant fact, **When** the person asks about it,
   **Then** the answer is streamed, each sentence using the fact ends with that fact's marker,
   and the final source list contains exactly the cited sources.
2. **Given** a question answerable only by arithmetic or general knowledge, **When** it is
   asked, **Then** the answer carries no citation markers and no source list is shown.
3. **Given** the assistant cites a marker that was never offered, **When** the answer is
   delivered, **Then** the system reports how many cited markers were ungrounded rather than
   silently trusting the assistant's own claim about what it cited.
4. **Given** a follow-up question in the same conversation, **When** it is asked, **Then** the
   assistant can use what was said earlier in that conversation and nothing from any other.
5. **Given** a question that is genuinely ambiguous in a way that would change the answer,
   **When** the assistant decides to ask for clarification, **Then** it presents 2-4 concrete
   interpretations as its final answer for that turn instead of guessing.
6. **Given** a message with an attached image, **When** the question is asked, **Then** the
   image is passed to the model as ordinary user content (not treated as retrieved data and not
   cited as a source).

---

### User Story 2 - Bad or unsafe input is stopped early and cheaply (Priority: P1)

Before the assistant spends any effort, the system checks the request itself. A request with no
verifiable identity is refused. An empty message is answered with a polite "I didn't receive a
question." A message that matches known prompt-injection or disallowed-content patterns, or that
a classifier judges to be an injection attempt, is answered with a short refusal. In every one of
these cases there is no knowledge-base lookup, no cache lookup, and no model call to answer the
message. One exception is disclosed rather than hidden: housekeeping of an over-long
conversation (Story 5) runs *before* screening, so a blocked message on a conversation that is
over its history ceiling can still trigger one summarization call for the *older* turns.

**Why this priority**: The assistant reads untrusted text on almost every turn. Cheap, early
refusal bounds both risk and spend, and is a precondition for trusting the rest of the pipeline.

**Independent Test**: Submit (a) a request with no identity, (b) an empty message, (c) a known
injection phrase, each on a short conversation. For each, confirm the user-visible refusal text,
a normal terminal outcome, and that no retrieval or answer-generation model call was made.

**Acceptance Scenarios**:

1. **Given** a request with missing or malformed identity, **When** a turn starts, **Then** the
   user is told the system could not verify who is asking, the event is counted, and nothing
   else runs. This is a system-level failure, kept distinct from a user-input error.
2. **Given** a message with no text and no image, **When** a turn starts, **Then** the user
   receives "I didn't receive a question — please try again."
3. **Given** a known injection/jailbreak phrasing (including slightly misspelled or
   singular/plural variants), **When** a turn starts, **Then** the user receives "I can't help
   with that request." and nothing downstream runs.
4. **Given** the injection classifier is unreachable, **When** a clean message arrives,
   **Then** the turn proceeds (screening degrades open), the degradation is counted, and a
   message that matches a known pattern is still refused.
5. **Given** an image-only message, **When** a turn starts, **Then** it is not screened for
   image content — the system screens words, not pixels (a disclosed gap).

---

### User Story 3 - Every turn is bounded and always ends (Priority: P1)

However the model behaves, a turn cannot run away. The system limits how many reasoning rounds a
turn may take, how many tool calls one round may request, how many identical consecutive tool
calls it tolerates, how many tokens and how many dollars one turn may spend, how long one tool
may take, and how long the whole turn may take. When a limit trips, the person receives a clear
message and a terminal outcome — never a silent hang, never an empty reply.

**Why this priority**: A tool-using model can loop, fan out, or stall. Unbounded behavior is the
failure mode that costs money and blocks workers; bounding it is what makes the assistant
operable at all.

**Independent Test**: Drive a scripted model that (a) loops on tool calls, (b) requests a dozen
tool calls at once, (c) repeats an identical call, (d) reports huge token usage. Confirm each
ends the turn with a user-visible message and exactly one terminal outcome, and that the ceiling
that fired is counted.

**Acceptance Scenarios**:

1. **Given** the model keeps requesting tools, **When** the round count reaches the ceiling
   (default 10), **Then** the turn ends with a user-visible message rather than an empty reply:
   the last content the model produced if it passes the same quality checks, otherwise a
   generic or reason-specific fallback.
2. **Given** the model requests more than 5 tool calls in one round, **When** the batch is
   inspected, **Then** none of them run, every pending call receives a "too many" response, and
   the model gets another round to make fewer, more targeted calls.
3. **Given** the model repeats an identical tool-call batch 3 times in a row, **When** the third
   repeat is requested, **Then** the turn ends (counted as "no progress") instead of exhausting
   the round ceiling.
4. **Given** cumulative usage in the turn reaches the token ceiling (default 16,000) or the
   cost ceiling (default $0.50), **When** the next round would begin, **Then** the turn ends
   with a user-visible message as in scenario 1. Usage by delegated sub-runs counts toward the
   same ceilings.
5. **Given** the model names a tool that does not exist, **When** the batch is inspected,
   **Then** it is rejected and retried without ever reaching a human or a tool.
6. **Given** a tool exceeds its time limit (default 15 s), **When** the limit passes, **Then**
   the model receives an error message it can react to and the turn continues.
7. **Given** the whole turn exceeds the wall-clock limit (default 60 s), **When** the next
   event is due, **Then** the stream ends with a timeout error, not a hang.
8. **Given** a new turn begins on a conversation that has already run turns, **When** it
   starts, **Then** all per-turn counters start from zero (but they are *not* reset when a
   paused turn resumes after an approval).

---

### User Story 4 - Poor answers are repaired or retried before the person sees them (Priority: P2)

After the model produces a candidate final answer, the system checks it. An answer is rejected
and retried when it is too short, when it narrates an intent to act instead of acting ("I will
use the X tool…", "would you like me to…?"), when it invents tool output that no tool call
backs, when it recites its own hidden instructions, when it skips a tool a loaded skill said
was required, when it cites a source for a claim the source does not support, or when it uses
retrieved facts without citing them. Two cleanups need no retry: a fabricated "References"
footer is removed, and a missing citation marker is inserted mechanically onto the sentence
that source clearly matches. If the same rejection reason repeats, the system stops retrying
and delivers a fallback — except when the only defect is a missing citation marker or an
answer that is short but real, in which case the answer it already has is kept.

**Why this priority**: Small local models fail in repeatable, observable ways. A quality gate
with a bounded retry converts those failures from user-visible defects into an extra round —
but it is an improvement over a working pipeline, not a prerequisite for one.

**Independent Test**: Feed the gate answers exhibiting each defect and confirm the reason code,
that a retry is requested (once per distinct reason), that a repeated reason ends the loop, and
that the client is told to discard text it has already streamed.

**Acceptance Scenarios**:

1. **Given** a candidate answer that defers instead of acting, **When** it is checked, **Then**
   it is retried with feedback naming the problem, and the client is told to discard what it has
   streamed so far.
2. **Given** the same rejection reason twice in a row, **When** the second is detected, **Then**
   no further attempt is made and the user receives a fallback — except for a missing-marker or
   too-short-but-non-blank answer, which is kept as is. An answer rejected for leaking its
   instructions, fabricating tool output, skipping a required tool, deferring, or misattributing
   a citation is always replaced, never kept.
3. **Given** an answer that uses a retrieved fact but omits its marker, **When** a source
   clearly matches a specific sentence, **Then** the marker is appended to that sentence
   automatically (several sources matching one sentence are appended together), the corrected
   text replaces the streamed text, and no retry is spent.
4. **Given** a cache-served or safety-net answer that never touched the model, **When** the turn
   ends, **Then** the user still receives the answer text (not a bare "done").

---

### User Story 5 - Conversations survive restarts and long histories stay bounded (Priority: P2)

A conversation's state is durable and shared across every process that serves it, so restarting
or redeploying never loses a conversation, and a conversation can continue on a different
process than the one that started it. When a conversation grows long, older whole turns are
folded into a running summary and dropped from the live history so the model's input stays
bounded; the person is told (informationally) that older messages were summarized. If the
summary itself grows past its limit, the turn ends with an explicit "start a new conversation"
message. Resuming a paused turn is allowed only against a checkpoint written by a compatible
build; otherwise it is refused with a named reason.

**Why this priority**: Without durability the approval gate (feature 003) is not a real safety
control, and without bounded history long conversations degrade silently. It matters, but a
short-lived single-process assistant still delivers Story 1.

**Independent Test**: Run a turn, discard the process, start a fresh one against the same store,
and confirm the history is intact. Then push a conversation past the history ceiling and confirm
trimming happens in whole turns, the summary accumulates, and an over-large summary ends the
turn with the named message.

**Acceptance Scenarios**:

1. **Given** a completed turn, **When** the serving process is replaced, **Then** the next turn
   on that conversation sees the earlier history.
2. **Given** raw history over the ceiling, **When** a turn starts, **Then** whole oldest turns
   are removed until the history is at or under the (lower) floor, no tool call is ever
   separated from its result, and the removed turns are folded into the running summary.
3. **Given** the summarization call fails, **When** compaction runs, **Then** the trim is still
   applied and only the summary update is skipped.
4. **Given** the running summary exceeds its character limit even after compaction, **When**
   the turn starts, **Then** it ends with an explicit "conversation has grown too long" message.
5. **Given** a resume request for a conversation that is not paused, or whose checkpoint was
   written by an incompatible build, **When** resume is attempted, **Then** it is refused with
   `checkpoint_lost` or `checkpoint_incompatible` respectively and counted — and a mere
   difference in build identity is *not* treated as incompatible.
6. **Given** a brand-new conversation, **When** its first turn runs, **Then** the fixed
   assistant instructions are seeded once — and a later turn that lands on a different or
   freshly restarted process does not seed them a second time, because the stored conversation
   is checked first rather than trusting in-process memory.

---

### User Story 6 - Dependency hiccups degrade the answer, not the turn (Priority: P2)

Auxiliary services fail in different ways, and each failure has a deliberately chosen policy:
enrichment degrades (a failed pre-fetch means a worse first guess, not a failed turn; a down
reranker means fused ordering; a down keyword leg means semantic-only search); the model call
itself is retried a few times because it has no fallback; a tool exception becomes a message the
model can react to; a security check fails closed. Every degrade-and-continue path is counted so
an operator can see it.

**Why this priority**: These are the paths that, in the project's own history, produced silent
hangs and quiet quality loss rather than crashes. Naming the policy per dependency is what keeps
them visible.

**Independent Test**: Inject a failure into each dependency in turn (pre-fetch, keyword search,
reranker, semantic cache, injection classifier, a tool, the model endpoint) and confirm the turn
still reaches a terminal outcome with the documented policy, and that the matching counter moves.

**Acceptance Scenarios**:

1. **Given** the pre-fetch fails, **When** a turn runs, **Then** it proceeds with no
   pre-fetched context, the assistant may still call the search tool, and the degradation is
   counted.
2. **Given** a transient model-endpoint error, **When** the model call fails, **Then** it is
   retried (up to 3 attempts) and a programming error is *not* retried.
3. **Given** a tool raises, **When** its result is needed, **Then** the model receives a short
   natural-language message and decides what to do next.
4. **Given** the keyword leg or the reranker is unavailable, **When** a search runs, **Then**
   results are still correct and still scoped to the caller, just lower-recall or
   lower-precision, and the stage that degraded is counted.

---

### User Story 7 - A repeated question is answered without redoing the work (Priority: P3)

When the same person asks something whose meaning is nearly identical to a question already
answered for them, the stored answer (and its sources) is returned immediately without
retrieval or a model call, and still passes through the quality gate. A cache failure of any
kind is just a miss.

**Why this priority**: Pure latency/cost optimization; correctness never depends on it.

**Independent Test**: Ask a question, let it complete, ask a near-identical one, and confirm no
retrieval and no model call occurred and the same sources are returned. Break the cache and
confirm the question is still answered normally.

**Acceptance Scenarios**:

1. **Given** a prior answer within the similarity threshold (default 0.95) and freshness window
   (default 1 hour), **When** the same person asks again, **Then** the stored answer is
   returned with its sources and nothing is re-generated.
2. **Given** the cache is unreachable or its index is missing, **When** a question is asked,
   **Then** it is treated as a miss and answered normally.
3. **Given** a turn that was itself served from cache, **When** it completes, **Then** it is
   not written back.

---

### Edge Cases

- A model request names a tool that is not registered → rejected and retried without a human
  ever seeing it.
- A model calls the skill-loading tool without first searching the skill catalog → rejected and
  retried (a hallucinated skill name is never trusted).
- The model does not report token usage → the token and cost ceilings never trip on usage that
  was not reported (fails open; the round and wall-clock ceilings still bound the turn).
- A retry round roughly doubles a turn's token spend → the token ceiling is sized for that
  headroom; a correctly cited answer must not be cut off by the budget before the quality gate
  sees it.
- Two different rejection reasons in a row → treated as slow progress, not a stuck loop; only
  the *same* reason repeating ends retries.
- An answer's own streamed text is replaced after the fact (retry, citation auto-fix, retry
  exhausted) → the client receives an explicit "discard what you rendered" signal first, so old
  and new text never concatenate.
- Final text produced by a node that never calls the model (refusal, cache hit, safety net) →
  still delivered as answer text.
- Delegated sub-runs stream their own internal reasoning → it never appears in the main answer
  stream.
- A new message arrives on a conversation that is paused at an approval → refused with a named
  error (specified in feature 003).
- Two concurrent turns on one conversation → the second is rejected as busy (specified with the
  queue transport, not here).

## Requirements *(mandatory)*

### Functional Requirements

**Turn entry and screening**

- **FR-001**: The system MUST establish the caller's verified identity once at the start of each
  turn and refuse the turn, before any retrieval, cache lookup or model call, when identity is
  missing or malformed. The refusal MUST be counted and MUST be distinguishable from an
  empty-input refusal.
- **FR-002**: The system MUST answer a message containing neither text nor an image with a fixed
  "I didn't receive a question" message and no further work.
- **FR-003**: The system MUST screen the message text for known injection/jailbreak phrasings, a
  small disallowed-content list, and (via a classifier) paraphrased injection attempts, before
  any cache lookup, retrieval or answer-generation model call. (Over-long-conversation
  housekeeping, FR-027, currently runs earlier — see *Known gaps*.) A positive match MUST fail
  closed; a failure of the screening mechanism itself MUST fail open and MUST be counted; a
  classifier outage MUST be counted separately from a screening bug.
- **FR-004**: Image content MUST NOT be assumed screened; image-only messages bypass screening
  and this MUST be stated as a limitation.

**Answering and citations**

- **FR-005**: The system MUST pre-fetch relevant knowledge (and the caller's own memories, per
  feature 002) before the assistant reasons, number every offered source, and frame the
  retrieved text as data — never instructions — with a standing rule in the assistant's fixed
  instructions saying so.
- **FR-006**: The assistant's fixed instructions MUST be identical for every caller and every
  request: no tenant, principal, timestamp or other per-request value may appear in them.
- **FR-007**: Document retrieval MUST combine semantic and keyword matching, then re-rank, and
  MUST drop results below a calibrated relevance floor so an unrelated source is never offered as
  support; results from the same parent document MUST be de-duplicated. A caller's own memories
  (feature 002) are small and are searched without re-ranking, hence without a floor.
- **FR-008**: The set of sources shown to the user MUST be computed from the final answer text
  intersected with the sources actually offered, never from the assistant's self-report; the
  count of cited markers that match no real source MUST be computed independently and reported.
- **FR-009**: The system MUST stream the answer incrementally and finish every turn with exactly
  one terminal outcome: done, approval-required (feature 003), or error.
- **FR-010**: The system MUST deliver cited sources and follow-up suggestions (only when the
  answer was grounded) immediately before the terminal outcome.
- **FR-011**: Optional image attachments MUST be passed to the model as user content, never
  fetched or decoded by the system.
- **FR-012**: A text-only message MUST be byte-identical to what it was before multimodal
  support existed (no behavior change for text-only callers).

**Bounds and safety budgets**

- **FR-013**: The system MUST end a turn that reaches the reasoning-round ceiling (default 10),
  the cumulative token ceiling (default 16,000), or the per-turn cost ceiling (default $0.50)
  with a user-visible message — the model's last content only if it still passes the quality
  checks, otherwise a fallback; a ceiling trip MUST be counted; usage by delegated sub-runs MUST
  count toward the parent's ceilings.
- **FR-014**: The system MUST reject a round that requests more than 5 tool calls, without
  running any of them, and let the assistant retry with fewer.
- **FR-015**: The system MUST end a turn, and count it as "no progress", after 3 consecutive
  identical tool-call batches within the same turn, independently of (and usually before) the
  round ceiling.
- **FR-016**: The system MUST reject a tool call naming a non-existent tool and a skill-load
  without a prior skill search, returning control to the assistant without involving a human.
- **FR-017**: Every rejected or skipped pending tool call (over-budget batch, invalid name,
  human rejection, cancellation) MUST receive exactly one response message so the next model
  call is well-formed.
- **FR-018**: A single tool call MUST be time-limited (default 15 s) and the whole turn
  wall-clock-limited (default 60 s); a tool timeout becomes a message to the assistant, a turn
  timeout becomes a terminal error.
- **FR-019**: Per-turn counters MUST reset at the start of every new turn and MUST NOT reset
  when a paused turn resumes. (Until #62 one counter — the running total of delegated sub-run spend — was not
  actually reset; see *Resolved since this spec was written*, bug B1.)
- **FR-020**: Tool results MUST have credentials scrubbed before reaching the assistant or any
  trace; tool results with no inherent row limit MUST carry a per-tool cap and be visibly marked
  when truncated.

**Quality gate**

- **FR-021**: The system MUST check every candidate final answer for: too-short content;
  deferring instead of acting; fabricated tool output; recitation of the hidden instructions; a
  skipped tool that a loaded skill required; uncited use of retrieved facts; and citations whose
  source shares no vocabulary with the citing sentence.
- **FR-022**: A rejected answer MUST be retried with feedback naming the problem, at most one
  self-correction per distinct reason; the same reason repeating MUST end retries. On exhaustion
  the existing answer MUST be kept only for a missing citation marker or a too-short but
  non-blank answer; every other reason (leaked instructions, fabricated output, skipped required
  tool, deferral, misattributed citation) MUST replace the answer with a fallback that names the
  category of problem without quoting the rejected content, and MUST NOT confirm a
  leaked-instructions detection to the user.
- **FR-023**: A fabricated reference-list footer MUST be stripped, and a missing citation marker
  MUST be inserted mechanically onto the sentence its source best matches, without spending a
  retry. Whenever streamed text is replaced, the client MUST first be told to discard what it
  rendered.
- **FR-024**: Every quality-gate rejection reason MUST be counted individually.

**Conversation durability and history**

- **FR-025**: Conversation state MUST be durable and shareable across processes; the fixed
  instructions MUST be seeded into a new conversation once, checked against the stored
  conversation (not in-process memory) so a restart or a different process never duplicates them.
- **FR-026**: Resume of a paused conversation MUST verify the conversation is actually paused
  (not merely mid-run) and was written by a compatible state schema; mismatches MUST be refused
  with `checkpoint_lost` / `checkpoint_incompatible` and counted. A differing build identity
  alone MUST NOT be a mismatch. A breaking change to the state shape or graph topology MUST bump
  the schema version.
- **FR-027**: History MUST be bounded: once estimated raw history exceeds a ceiling (default
  24,000 tokens), whole oldest turns MUST be removed until at or under a lower floor (default
  4,000), never splitting a tool call from its result, never dropping the fixed instructions or
  the current turn; removed turns MUST be folded into a cumulative summary that persists for the
  life of the conversation.
- **FR-028**: A summary exceeding its limit (default 4,000 characters) after compaction MUST end
  the turn with a named "conversation too long" outcome, never silent truncation.
- **FR-029**: A summarization failure MUST still apply the trim and skip only the summary update.
- **FR-030**: A conversation transcript MUST be replayable for a session switcher, showing user
  and assistant text and a marker where older turns were summarized, omitting tool plumbing.

**Degradation, errors and observability**

- **FR-031**: Each dependency MUST have a documented failure policy: pre-fetch and cache degrade;
  keyword leg and reranker degrade with scoping intact; the model call is retried (3 attempts,
  transient errors only); tool errors become messages; a security check fails closed.
- **FR-032**: Every degrade-and-continue path MUST increment a counter.
- **FR-033**: Caller-facing errors MUST use a single `{code, message, details}` envelope drawn
  from a closed code registry (timeout, moderation_blocked, checkpoint_lost,
  checkpoint_incompatible, pending_approval, unattended_pause, cancelled, cost_ceiling_exceeded,
  tenant_budget_exceeded, thread_busy, no_progress, worker_lost, internal — six are registered
  but not emitted and three `error` paths bypass the envelope, see *Known gaps*); a tool's message
  to the assistant MUST remain natural language and not use the envelope.
- **FR-034**: Logs and traces MUST carry metadata only (node, run id, duration, outcome) and
  never message content or the full state; each node MUST emit start, complete, fail and — for an
  approval pause — "paused" records, where a pause is not a failure.
- **FR-035**: A semantically near-identical earlier answer for the same caller (default
  threshold 0.95, default freshness 1 hour) MUST be returnable without retrieval or a model call,
  MUST still pass the quality gate, MUST NOT be re-cached, and its absence or failure MUST be a
  plain miss.

### Key Entities *(include if feature involves data)*

- **Conversation**: A durable, identified exchange between one caller and the assistant;
  carries its message history, a cumulative summary of trimmed turns, and a state-schema version.
- **Turn**: One question and everything the system does until a terminal outcome; owns its own
  counters (rounds, tokens, cost), run id, and quality-gate verdict. Starts at zero every time.
- **Source (Citation)**: A numbered piece of retrieved material offered to the assistant for one
  turn; has an index, a display snippet, and an origin. The *used* subset is derived from the
  answer text.
- **Safety Budget**: The set of named ceilings that bound a turn (rounds, tool fan-out, repeats,
  tokens, cost, tool time, turn time, history, summary).
- **Quality Verdict**: The reason (if any) a candidate answer was rejected, and how many
  consecutive times that same reason has fired.
- **Conversation Summary**: Cumulative text standing in for trimmed turns; bounded by a character
  limit; never reset between turns.
- **Error Envelope**: The `{code, message, details}` shape of every caller-facing failure.
- **Cached Answer**: A previously delivered answer and its sources, keyed by meaning and by
  caller, with a freshness window.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of turns, under normal operation and under each injected single-dependency
  failure, end in exactly one terminal outcome (answer, approval request, or error) within the
  turn time limit; no turn hangs and none returns an empty reply.
- **SC-002**: No turn exceeds 10 reasoning rounds, 16,000 tokens or $0.50 of model spend,
  regardless of model behavior (verified against scripted looping, fan-out and runaway-usage
  models).
- **SC-003**: For 100% of requests refused at entry (no identity, empty message, injection or
  disallowed content) on a conversation within its history ceiling, zero knowledge-base lookups,
  cache lookups or answer-generation model calls occur.
- **SC-004**: At least 95% of citation markers across the golden evaluation set are real
  (match an offered source), computed from the answer text and not from the assistant's
  self-report.
- **SC-005**: After a process restart, 100% of previously completed conversations resume with
  their full history intact, and a conversation started on one process can be continued on
  another.
- **SC-006**: A conversation of unbounded length never sends the model more raw history than the
  ceiling allows, and a conversation that cannot be compacted further ends with an explicit
  message rather than degrading silently.
- **SC-007**: A near-identical repeat question for the same caller within the freshness window is
  answered with no retrieval and no model call; with the cache broken, the same question is still
  answered correctly.
- **SC-008**: For each of the seven dependencies listed in User Story 6's independent test
  (pre-fetch, keyword search, re-ranker, answer cache, injection classifier, a tool, the model
  endpoint), an injected failure still yields a completed turn under the documented policy and
  moves the matching counter.
- **SC-009**: Every quality-gate rejection reason, every safety-ceiling trip and every
  degrade-and-continue path is visible as a counter, so an operator can answer "how often, across
  everyone" without reading transcripts.

## Assumptions & Known Gaps

**Assumptions**

- Callers reach this pipeline through a trusted layer that has already authenticated them and
  supplies their tenant and principal; the pipeline refuses to guess either (see feature 002).
- The model, embedding and re-ranking services run locally and are reached through a single
  gateway using logical aliases, never provider model names; the pipeline works fully offline.
- Numeric defaults (rounds 10, tool fan-out 5, repeats 3, tokens 16,000, cost $0.50, tool 15 s,
  turn 60 s, history 24,000/4,000, summary 4,000 chars, cache 0.95/1 h) are tunables, not
  contracts; the *existence* of each bound is the requirement.
- English-language questions; no localisation is assumed.
- Cost is approximate: models without a price entry (every locally run model) cost $0, so the
  cost ceiling only bites against a metered provider.

**Out of scope for this feature** (specified elsewhere or not yet specified)

- Tenant/owner isolation, identity headers and cross-session memory → `002-tenant-isolation-and-memory`.
- The approval gate, side-effecting tools, cancellation and crash recovery → `003-approval-and-exactly-once-writes`.
- The queue/worker transport, per-domain stacks, the per-tenant daily budget, document ingestion,
  skills and sub-agents, observability stack — not covered by the first spec batch.

**Known gaps (disclosed, not hidden)**

- **G2 —** Image-only input is not screened (words, not pixels).
- **D1 — Screening runs after history housekeeping, not first.** The constitution (Principle VI) says
  screening runs before any retrieval or model spend; as built, an over-ceiling conversation's
  summarization call happens before screening. It is bounded (once per ceiling trip, over old
  turns, never over the new message) but it is a literal deviation. See `plan.md` Complexity
  Tracking.
- **G3 — The answer cache is keyed on the latest message text only.** Found by reading the code, not
  reproduced, and not mentioned in the existing docs: a context-dependent follow-up ("pls be more
  detailed", "and for support?") can match an answer cached for an unrelated earlier conversation
  of the *same* caller, because the cache lookup runs before retrieval and never sees the
  conversation. It is scoped to the caller, so it is a wrong-context risk, not a cross-tenant
  one; the freshness window (default 1 hour) bounds it. See `research.md` R16.
- Token/cost ceilings fail open when the model does not report usage.
- The injection screens are pattern- and classifier-based, honestly scoped to "known and
  paraphrased attempts," not a claim of understanding intent.
- Approximate token estimation drives history trimming; the estimator is not the model's own
  tokenizer.
- **A2 — The error envelope is not universal (checked against every `error` emitter).** Six registry
  codes are never emitted: four (`moderation_blocked`, `cost_ceiling_exceeded`, `no_progress`,
  `unattended_pause`) because those outcomes are delivered as a user-visible message plus a normal
  `done`, distinguishable only by counters; and two (`checkpoint_lost`, `checkpoint_incompatible`)
  because a refused resume is an `error` event whose *text* starts with the code name but which
  carries no `code` field. Separately, one more `error` path bypasses the envelope: the wait for a first worker event
  timing out ("is an agent-worker running for this domain?"). A client therefore cannot reliably
  branch on `code`. (The worker's catch-all, which forwarded raw exception text to the caller, was
  fixed in #64 — see *Resolved since this spec was written*.) See `contracts/error-envelope.md`.
- First-use seeding of a conversation's fixed instructions checks stored state first, but the
  check-then-write is not atomic; correctness relies on the one-active-turn-per-conversation
  guarantee above.
- Concurrent writers to one conversation are serialized by the transport tier, not by this
  pipeline; this feature assumes at most one active turn per conversation.

### Resolved since this spec was written

These were *Known gaps* in the first version of this spec. Each was fixed test-first in a later pull request; the findings are kept so the history is not lost.

- **Bug B1 — delegated sub-run spend was never reset between turns — fixed in #62.** The running list was declared with an append-only reducer, so the per-turn "reset to empty" added nothing and earlier turns' spend kept counting against later turns' ceilings (reproduced: `[(3000, 0.25)]` still stored after turn 2 began). The unit test for the reset asserted the node's *return value*, which cannot see a reducer. The list now has a reset-aware reducer (`_concat_or_reset` in `app/agent/graph.py`: concatenate, or `None` resets) that stays race-free for parallel delegations; `validate_input` writes `None`. Regression tests assert the state read back from the compiled graph across two turns: `tests/agent/test_safety_budgets.py::TestPerTurnResetThroughTheGraph` and `TestConcatOrResetReducer`. No state-schema bump (no field added or removed).
- **A2, the worker catch-all — fixed in #64.** Any unexpected exception put its own text into the caller-facing `error` event (a driver message naming an internal host, a SQL fragment, a library echoing input) — in the stream core, in the legacy stream and in the worker's catch-all, which also carried no `code`. One builder, `internal_error_envelope(exc)` in `app/core/errors.py` (a fixed message plus `details={"error_class": …}`), now serves all three; the full text stays on the trace and the log line carries the class only. Tests: `tests/agent/test_streaming_terminal_events.py::TestAGraphFailureNeverLeaksItsMessageToTheCaller` and the updated `tests/job_queue/test_agent_worker.py` (four tests had asserted the leak). **Still open under A2**: the six unemitted codes, a refused resume without a `code`, and the first-event deadline (tasks T081–T083). The *ingest* worker's catch-all still forwards `str(exc)` — feature 006, B10.
- **A3, the answer cache's tag escaping — made explicit in #68 and a real defect fixed in #69.** #68 added hermetic tenant- and principal-scoping tests (`tests/retrieval/test_semantic_cache.py`). Writing them exposed that the escape set did not cover `|` (the OR operator inside a tag block) or the backslash, so a principal such as `alice|bob` built a filter that also matched `bob`'s cached answers — a Principle I defect (exploitable only where a principal string is user-influenced; the trusted-header seam already lets a caller *claim* an identity). #69 escapes every ASCII character that is not a letter, digit or underscore, with `tests/retrieval/test_semantic_cache_tag_escaping.py` and a real-Redis `tests/integration/test_semantic_cache_tag_escaping_real_redis.py`.
