# Feature Specification: The CLI and the Telegram channel go through the front door

**Feature Branch**: `feat/channels-behind-the-front-door`

**Created**: 2026-10-31

**Status**: Draft

**Input**: The maintainer's request of 2026-10-31: move the CLI (`make chat`) and the Telegram channel behind the front door of spec 018, as clients of the same chat routes the web UI uses, each acting as a real person in the authorization server (one person per CLI user, one per Telegram user). Decided in conversation the same day: two PRs (the diagram and README caveat first, #224, then this), and one authorization-server user per person rather than a service credential that may act as anyone, or moving only the CLI.

## Why

Spec 018 put one door in front of the chat API: it proves who is calling, charges that person's and that tenant's rate limits, honours revocation, and hands the API a short-lived signed identity that the API trusts instead of anything the client says. Two of the three ways to talk to the agent do not use it.

The CLI and the Telegram channel (pattern 89) publish their turns straight to the work queue from their own process, and each builds its own identity: a fixed tenant plus the operating-system user (CLI) or the Telegram user id (Telegram). Pattern 89 chose that on purpose, to give them the worker's guarantees (the per-conversation lock, crash reclaim, the dead-letter archive) without a second execution path, and wrote "no authorization in the producer, on purpose". That was right for what it fixed and leaves three gaps that spec 018 exists to close:

1. **No limits.** The per-tenant and per-person buckets never see these turns. Telegram is the one surface that reaches the public internet, and nothing meters it.
2. **No proof of who.** The identity is whatever the process says it is. Anything that can write to the queue can name any tenant and any person, so tenant isolation (Principle I) for these two doors rests on who holds the queue's credentials, not on a proven identity.
3. **No revocation.** Revoking a person at the door does nothing to the same person on Telegram.

This feature makes all three ways in (web, CLI, Telegram) pass the same door, so there is one place that proves identity, meters it and can cut it off. It must not give up what pattern 89 bought: the worker's guarantees, Telegram's refusal to ever approve a pause, and its safety against a redelivered message. The README states the present exception ("the CLI and the Telegram channel never pass through the front door", PR #224); this feature removes it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A person chats from the terminal as themselves, through the door (Priority: P1)

A person runs `make chat`. The first time, they sign in with the same sign-in the web UI uses; after that the terminal stays signed in until that sign-in expires. Their messages reach the agent carrying their proven tenant and person, and the answer streams back as it does today, with approval prompts under `make chat-hitl` and a clean stop on Ctrl-C.

**Why this priority**: It is the smallest slice that proves the whole path (sign-in, door, API, worker, stream) for a client that is not a browser, and it does not depend on how Telegram people are created. It can ship alone.

**Independent Test**: Start the front-door stack, sign in at the terminal as a person of tenant A and, in another terminal, as a person of tenant B, ask each the same question about stored records, and check that each sees only their own tenant's rows. Repeat with a forged tenant id, person id and identity header supplied by the terminal's own code and check that none changes who the turn runs as.

**Acceptance Scenarios**:

1. **Given** a signed-in person of tenant A, **When** they send a message from the terminal, **Then** the turn runs as that tenant and that person, and the answer contains only tenant A's data.
2. **Given** a terminal that is not signed in, **When** the person sends a message, **Then** they are asked to sign in and nothing is sent to the agent.
3. **Given** a signed-in person whose sign-in has expired, **When** they send a message, **Then** it is renewed without asking when it can be, and otherwise they are asked to sign in again; a reply already streaming is never cut by the expiry.
4. **Given** a turn in progress, **When** the person presses Ctrl-C or ends the input, **Then** the worker is asked to stop that turn, as today.
5. **Given** a turn that pauses for approval under `make chat-hitl`, **When** the person approves or rejects, **Then** the turn resumes through the door and the tool runs or not, as today.

---

### User Story 2 - Every Telegram person is a distinct, provable person (Priority: P1)

A person messages the Telegram bot. The first time, the bot creates a person for them in the authorization server (inside the one tenant the bot serves); from then on every message from that Telegram user reaches the agent as that person, proven by the door, and two Telegram users never share a person or each other's memories.

**Why this priority**: Without it Telegram cannot go through the door at all, and it is the surface that most needs the door.

**Independent Test**: Two Telegram accounts message the bot; check that each gets its own person, that one cannot see what the other said or stored, and that a restart of the bot gives the same two people. Revoke one at the door and check that only that one is refused.

**Acceptance Scenarios**:

1. **Given** a Telegram user who has never messaged the bot, **When** they send a message, **Then** exactly one person is created for them and the turn runs as that person.
2. **Given** the same user messaging again after a bot restart, **When** they send a message, **Then** it runs as the same person.
3. **Given** ten simultaneous first messages from one new user, **When** they arrive, **Then** exactly one person exists afterwards.
4. **Given** a person revoked or disabled at the door, **When** their Telegram user messages the bot, **Then** the turn is refused, they are told they are not allowed, and the bot does not re-create or re-enable them.
5. **Given** a person created for a Telegram user, **When** anyone tries to sign in as them at the web UI or the terminal, **Then** they cannot: that person has no sign-in a human could use or guess.

---

### User Story 3 - A Telegram turn can never approve a pause (Priority: P1)

A Telegram message asks for something that needs a person's approval (any non-read-only tool). The turn stops, nothing runs, the person is told it needs approval this channel cannot give, and the conversation is left ready for the next message, not paused for good. The same holds however the request reaches the door: the channel says the turn is unattended, and no value of that statement can make a turn approve anything.

**Why this priority**: Principle II is non-negotiable, and today it holds because the worker is told directly. Moving to the door must not open a path where a pause is approved or left stuck.

**Independent Test**: Send, over Telegram, ten prompts that each lead the agent to a mutating or outward tool; check that no write happens, each reply says it needs approval, and the next message in each conversation is answered normally. Send the same prompts marked attended from the terminal under `make chat-hitl` and check they pause and wait for the person.

**Acceptance Scenarios**:

1. **Given** an unattended turn that reaches an approval pause, **When** the worker gets to it, **Then** the pause is declined, nothing runs, and the conversation is not left paused.
2. **Given** any request marked unattended, **When** it is processed, **Then** it can only be stricter than an ordinary turn; nothing it carries can approve a call or skip the gate.
3. **Given** a turn from a worker that does not understand the mark yet, **When** it ends at a pause anyway, **Then** the channel cancels the pause itself and says nothing was done, as it does today.

---

### User Story 4 - A redelivered Telegram message is one turn, not two (Priority: P2)

Telegram redelivers an update after the bot crashed between replying and noting that it had. The agent answers that message once. Telegram numbers its updates, and the bot sends that number with the turn so that a replay is recognised as the same attempt and streams the first attempt's events.

**Why this priority**: Pattern 89 found that once a turn outlives the channel, a replay is a second model call and a second copy of the question in the conversation. It is a correctness property, not a nicety, but it does not block the other three.

**Independent Test**: Deliver the same update to the bot five times, including once after a simulated crash between replying and saving the offset, and check that exactly one turn ran and the person got one reply. Then send the same replay key as a different person and check that it never returns the first person's turn.

**Acceptance Scenarios**:

1. **Given** a turn started with a replay key, **When** the same person replays it for the same conversation within the window, **Then** no second turn starts and the first attempt's events are streamed.
2. **Given** a replay key used by one person, **When** another person (same tenant or not) sends the same key, **Then** it is treated as their own new key and they never receive the first person's turn.
3. **Given** a publish that failed after the key was claimed, **When** the same key is sent again, **Then** the turn runs (the claim is given back).

---

### User Story 5 - The limits apply, and the person is told (Priority: P2)

A person (or a tenant) who sends too many turns from the terminal or Telegram is refused at the door with a wait time, which the channel shows in plain words ("try again in 12 seconds"). Cancelling a turn is never throttled, so a person is never stuck unable to stop a runaway turn (answering an approval counts as a turn, as at the web UI).

**Why this priority**: It is the gap named first in Why, but it follows once the channels are behind the door.

**Independent Test**: Drive one Telegram person past their limit and check that their next message gets a wait-time reply while a second Telegram person and a web user of the same tenant are still served up to the tenant's own limit; cancel a turn during the throttle and check it succeeds.

**Acceptance Scenarios**:

1. **Given** a person over their limit, **When** they send another turn, **Then** it is refused before the API, the reply says how long to wait, and no turn runs.
2. **Given** a person over their limit, **When** they cancel a running turn, **Then** the cancel succeeds.
3. **Given** many Telegram people in one tenant together exceeding the tenant's limit, **When** another person of that tenant sends a turn, **Then** it is refused with a wait time like any other (see Assumptions: the tenant bucket is shared).

---

### User Story 6 - When something is down, the person is told, and nothing goes around the door (Priority: P2)

If no worker serves the domain, the person is told so, in the words they get today. If the front door or the authorization server cannot be reached, or refuses the sign-in, the channel says it cannot verify the person right now. Neither channel falls back to publishing to the queue itself.

**Why this priority**: A fail-open fallback would silently reinstate every gap in Why.

**Independent Test**: Stop the worker, send a message from each channel, and check for a message naming the missing worker. Stop the door, do the same, and check that each channel reports it and that no turn appears on the queue.

**Acceptance Scenarios**:

1. **Given** no worker for the domain, **When** a person sends a message, **Then** they see a message saying no agent worker is serving it, within the existing first-response deadline.
2. **Given** the door unreachable, **When** a person sends a message, **Then** they see a message saying their sign-in could not be checked, and no turn is queued.
3. **Given** the worker dies mid-turn, **When** the lock, reclaim and dead-letter machinery runs, **Then** the outcome is the one it has today (unchanged).

---

### User Story 7 - Development still works without the whole stack (Priority: P3)

A developer who runs the API directly in header mode (explicitly chosen, loudly announced, never the default) can still use the terminal and the bot against it, naming themselves as the web UI can there. The hermetic test suite needs no running services.

**Why this priority**: Without it, `make chat` would need Casdoor, the door and Caddy for every contributor.

**Independent Test**: With the API in header mode and no door, run the terminal and check it works; run the default hermetic suite and check it passes with no service running.

**Acceptance Scenarios**:

1. **Given** the API in header mode, **When** the terminal or the bot sends a turn, **Then** it runs as the identity the channel names, and the existing header-mode announcement and metric are unchanged.
2. **Given** the API in identity mode (the default), **When** a channel sends a name of its own making, **Then** it is ignored, as for every other client.

### Edge Cases

- **A Telegram user is revoked at the door while the bot holds their sign-in.** The next request is refused; the bot must not mint a replacement.
- **The authorization server is down when a new Telegram user writes.** They are told to try again; no half-created person is left behind, and the retry creates exactly one.
- **A token expires in the middle of a long answer.** The answer finishes (spec 018 A7); the next request renews.
- **The bot's stored sign-ins leak.** They must not allow acting as a person outside the Telegram group, in another tenant, or on any route but chat (FR-005).
- **A web client tries to use a Telegram conversation id.** It is refused, as the reserved namespace is today; a Telegram person can use that namespace and no other.
- **The terminal user is a different operating-system user from the person they sign in as.** The signed-in person wins; the operating-system name is no longer an identity.
- **Two terminals, one person, one conversation id.** The worker's per-conversation lock serialises them, as today.
- **Telegram retries an update the bot already answered, after the replay window.** Today's limit stands (300 s); the spec does not widen it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The CLI and the Telegram channel MUST send every turn, approval answer and cancel to the chat API through the front door, and MUST NOT write to the work queue, the results streams or the cancel flags themselves. A test fails the build if either channel can reach the work queue or the results directly (it replaces the check that they do not import the graph).
- **FR-002**: The tenant and person a turn runs as MUST be the ones the front door signs from the caller's token. Neither channel supplies them, and the API in identity mode ignores any the channel sends (spec 018's header stripping).
- **FR-003**: The CLI MUST sign a person in with the web UI's sign-in, keep them signed in across runs until the sign-in's own lifetime ends, renew it when it can, and let them sign out. The stored sign-in MUST be readable only by that operating-system user and MUST never be printed, logged or put in an error.
- **FR-004**: Each Telegram user MUST map to exactly one person in the authorization server, in the one tenant the bot serves, created on that user's first message; the same Telegram user MUST always map to the same person, and two Telegram users MUST never share one. Concurrent first messages MUST create one person.
- **FR-005**: A person created for a Telegram user MUST have no interactive sign-in a human could use or guess. The sign-ins the bot holds MUST be limited to the chat routes, to people in the Telegram group of that one tenant, and MUST NOT be usable for any other person, other tenant or other route.
- **FR-006**: A revoked or disabled person MUST be refused on both channels like on any other route; the bot MUST NOT create a replacement, re-enable them or hide the refusal.
- **FR-007**: A chat request MUST be able to say the turn is unattended. An unattended turn that reaches an approval pause MUST decline the pause (never approve it), run nothing, and leave the conversation resumable. The statement MUST only ever make a turn stricter; no request value may approve a call or bypass the gate (constitution Principle II).
- **FR-008**: Every Telegram turn MUST be sent as unattended. The CLI MUST send ordinary (attended) turns, with approval prompts under `make chat-hitl` as today.
- **FR-009**: A chat request MUST be able to carry a replay key. Within the replay window a second request with the same key, from the same person, for the same conversation MUST NOT start another turn and MUST stream the first attempt's events. A key MUST be scoped to the person who sent it: another person with the same key MUST get their own turn, never the first person's. A claim whose publish failed MUST be released.
- **FR-010**: Telegram MUST send its update number as the replay key, so a redelivery after a crash is one turn, for at least as long as the window today (300 s).
- **FR-011**: Turns and approval answers from these channels MUST count against the tenant's and the person's limits; cancels and reads MUST NOT be throttled. A refusal MUST reach the person as a message that includes the wait time.
- **FR-012**: When no worker serves the domain, the door or the authorization server is unreachable, or the sign-in is refused, the person MUST be told what is wrong, in plain words and within the existing first-response deadline. A channel MUST NOT fall back to any path that skips the door.
- **FR-013**: The terminal MUST ask the worker to stop the turn on Ctrl-C or end of input, as today.
- **FR-014**: The worker's guarantees (the per-conversation lock, crash reclaim, the dead-letter archive, unattended handling on the job) MUST be unchanged and MUST cover turns from these channels exactly as before.
- **FR-015**: The reserved Telegram conversation namespace MUST stay reserved: only a person created for a Telegram user may use it, and such a person MUST NOT use any other. Conversation ownership checks MUST apply to these channels as to the web UI.
- **FR-016**: When the API is explicitly in header mode, the channels MUST work against it as the web UI does there, and the mode's announcement and metric MUST be unchanged. The default hermetic suite MUST pass with no service running.
- **FR-017**: The channels MUST count what is new (sign-ins renewed and failed, people created, refusals by reason, unattended pauses) in metrics whose names are literals, and no log, metric label or error MUST contain a token, a key or a password.
- **FR-018**: `GRAPH_PATTERNS.md` pattern 89 MUST be rewritten to say what is now true (the exception is gone, the clause "no authorization in the producer, on purpose" is replaced by where authorization now happens), the README's statements that these two channels skip the front door and its limits MUST be removed, and the architecture diagram MUST show both channels entering through Caddy.

### Key Entities

- **Person**: someone the authorization server knows, inside a tenant. The CLI's user is a person who signs in; a Telegram user is a person the bot created.
- **Channel sign-in**: what lets a channel act as one person: the terminal's stored sign-in, or the bot's per-person sign-in. It is the only thing a channel presents to the door.
- **Telegram link**: the one-to-one record of which Telegram user is which person. It outlives a restart.
- **Unattended mark**: a statement on a chat request that nobody can answer an approval. It narrows a turn and nothing else.
- **Replay key**: a caller-chosen identifier that makes a repeated request the same attempt. Scoped to the person.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With people of two tenants signed in on each channel, each sees only their own tenant's data, and 0 of at least 8 spoofing variants per channel (forged tenant, person, identity header, duplicate headers, header casing) change who a turn runs as.
- **SC-002**: With the front door stopped, a message from each channel produces a visible failure within the first-response deadline and 0 turns appear on the work queue; with the door running, 0 turns from these channels reach the queue by any other route.
- **SC-003**: A person driven past their limit gets a reply that states a wait time on their next turn, a second person and the web UI are unaffected up to the tenant's limit, and cancels succeed 100% of the time during the throttle.
- **SC-004**: Across at least 10 approval-gated prompts sent over Telegram, 0 writes happen, 0 conversations are left paused, and the next message in each is answered normally.
- **SC-005**: Delivering the same Telegram update 5 times, including once after a simulated crash before the offset was saved, runs exactly 1 turn and gives 1 reply; the same key from a second person never returns the first person's turn.
- **SC-006**: A person revoked at the door is refused on both channels within 5 seconds (spec 018 SC-007) and the bot does not re-create them.
- **SC-007**: Ten concurrent first messages from one new Telegram user create exactly 1 person; two distinct new users create 2.
- **SC-008**: A sign-in extracted from the bot's stored state cannot be used at the web UI, for another tenant, for a non-Telegram person, or on any route but chat (verified by trying each).
- **SC-009**: The time added to a turn's first token by going through the door, measured on the local stack against the direct-to-queue path and recorded in the research notes, has a median under 100 ms.
- **SC-010**: A search of every service's logs and of the terminal's and bot's output after the end-to-end run finds no token, key or password.
- **SC-011**: The default hermetic suite passes with no service running; every deliberate defect on a mutation list for the new checks (identity taken from the channel, unattended mark approving, replay key shared across people, fallback to the queue when the door is down, a bot sign-in valid beyond chat) is caught by a test.
- **SC-012**: Delivery is split into PRs along the task phases. Each one's hand-written line count is recorded in its description; where it exceeds the repository's guidance the description says so and names what would be split off first.

## Assumptions

- **A1 (not deployed, so no migration).** The app has never been deployed (maintainer, 2026-10-29). Conversations and people from before this change are not carried over, no compatibility shim keeps the old direct path, and the existing channel files may be rewritten.
- **A2 (one person per person).** Decided 2026-10-31: each CLI user is a person who signs in, and each Telegram user is a person the bot creates. Rejected alternatives: one service credential that may act as any Telegram user (a new, powerful door feature and a threat-model amendment), and moving only the CLI.
- **A3 (Telegram people).** They live in the single tenant the bot serves today, in a group that cannot sign in interactively. Any Telegram user who can message the bot gets a person on first message, as any Telegram user can today; an allow-list is out of scope, and becomes possible because the door can now revoke.
- **A4 (open research item R1: how the bot gets a sign-in for a person it created).** The bot must be able to obtain a sign-in for a person without that person signing in, and without that sign-in being usable by anyone else (FR-005). Whether and how the authorization server (Casdoor 4.24.0, measured in spec 017) allows this is the first item of the plan. **If it cannot be done safely, the Telegram half is revisited with the maintainer; the terminal half (User Story 1) stands on its own and is the first PR.**
- **A5 (production wiring).** The production Caddyfile and compose are not wired to the door yet (spec 018 task T069, the maintainer's). This feature is proven on the local stack and in the hermetic suite; until T069 lands, production has the same exposure as before.
- **A6 (header mode).** In an explicitly chosen header mode the channels follow the API's mode (FR-016). Identity mode is the default, as in spec 018 A9.
- **A7 (limits).** Defaults stay as in spec 018 (30 a minute per tenant, 10 a minute per person). Telegram people share their tenant's bucket with everyone else in that tenant, so a busy bot can use up the tenant's allowance for the web UI; the per-person bucket bounds any one Telegram person. Giving the Telegram group its own tenant or bucket is a follow-up, not part of this feature.
- **A8 (token expiry).** As in spec 018 A7, expiry is checked once per request; a reply already streaming is never cut by it.
- **A9 (out of scope).** Linking one human's Telegram and web accounts into one person; an allow-list for the bot; wiring production to the door; a second messaging channel; changing the web UI.
- **A10 (what it amends).** Pattern 89's "no authorization in the producer, on purpose" and its description of the channels as trusted processes; the README's present exception; the diagram labels "straight to Redis, outside the front door". Principle I (tenant isolation) is touched, so the plan's Constitution Check must address it, and Principle II is restated to hold on the new path (FR-007, FR-008).
