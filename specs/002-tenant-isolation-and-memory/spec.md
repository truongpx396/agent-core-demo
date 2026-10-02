# Feature Specification: Tenant Isolation and Cross-Session Memory

**Feature Branch**: `002-tenant-isolation-and-memory`

**Created**: 2026-10-02

**Status**: Implemented (retrospective) — with one verified isolation gap, see *Known gaps* B2

**Input**: User description: "Tenant isolation and cross-session memory (retrospective spec of the as-built system): every read and write is scoped to the caller's tenant (and, for personal data such as memories and conversations, to the owner); identity is stamped once at a trusted boundary and never derived from message content, tool arguments or model output; missing or malformed identity is refused, never defaulted. Users can have the assistant remember facts across conversations, but only through an explicit, approved write; remembered facts are re-filtered by tenant and owner on every recall, expire after a retention period, and can be deleted by an operator with an audit trail."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01, commit
> `6089718`) from the code, the constitution (Principle I is NON-NEGOTIABLE, Principle VI covers
> memory) and `GRAPH_PATTERNS.md` patterns 17, 18, 19, 21, 22, 33. It describes what the system
> does today. Where the as-built behavior falls short of the principle it is meant to satisfy, the
> shortfall is stated in *Known gaps* with how it was established — one of them (B2) by running a
> hermetic reproduction. Companion specs: `001-core-rag-agent-turn` (the pipeline that consumes
> identity) and `003-approval-and-exactly-once-writes` (the gate every memory write passes).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - One customer never sees another customer's data (Priority: P1)

An organization (a **tenant**) shares one deployment with other organizations. When a person from
tenant A asks a question, everything the assistant looks up on their behalf — knowledge-base
documents, structured records such as a staff directory, stored answers, and the list of past
conversations — comes only from tenant A. A person can narrow a lookup (by topic, by department,
by specific document) but can never widen it past their tenant.

**Why this priority**: This is the reason the rest of the system can be shared. A single
cross-tenant leak is a customer-visible data breach, and the code deliberately makes the correct
and the buggy implementation look identical until a leak happens on an untested query, so the
boundary has to be structural rather than a convention.

**Independent Test**: Seed two tenants with similar content. Ask the same questions as each.
Confirm each only ever receives its own documents, directory rows, cached answers and session
list, including when the question names a topic, a department, or another tenant's document id.

**Acceptance Scenarios**:

1. **Given** tenants A and B each have documents on the same topic, **When** a person from A
   searches, **Then** only A's documents are candidates — B's are excluded by the lookup itself,
   not filtered out of an already-fetched result.
2. **Given** a lookup narrowed to a specific document id belonging to tenant B, **When** a person
   from A makes it, **Then** it returns nothing: the narrowing only intersects with A's scope.
3. **Given** a structured-data question ("who works in Support?"), **When** asked from tenant A,
   **Then** only A's rows are returned and the answer is capped, with a visible marker when the cap
   truncates it.
4. **Given** the same person asks a near-identical question twice, **When** the second answer is
   served from the stored-answer cache, **Then** the cache entry belongs to that same tenant *and*
   that same person — never another person in the tenant.
5. **Given** a child record (a ticket comment, a lead note) exists, **When** it is read, **Then**
   it carries and is filtered by its own tenant rather than inheriting it only through its parent.
6. **Given** identical content uploaded by two different tenants, **When** both are ingested,
   **Then** each tenant gets its own independent records; one never overwrites the other.

---

### User Story 2 - Identity is established once, at the edge, and cannot be talked into changing (Priority: P1)

The system learns who is asking exactly once, at a trusted boundary (a gateway-supplied pair of
identity headers for web callers; the operating-system user for the command line; the chat-app user
id for Telegram; a fixed ingest identity for seeding). That identity is then read-only for the
whole turn. It cannot be set by anything the person types, by an argument a tool receives, or by
what the model outputs. If identity is missing or malformed, the request is refused — there is no
default tenant and no anonymous mode.

**Why this priority**: Every isolation guarantee above is only as strong as the identity it keys
on. An identity the model or a message body could influence would be a privilege-escalation
primitive, not a parameter.

**Independent Test**: Send requests with (a) no identity headers, (b) an empty tenant, (c) a
message body that *claims* a different tenant, (d) a model-proposed tool call containing a tenant
argument. Confirm (a) and (b) are refused, and (c) and (d) have no effect on whose data is touched.

**Acceptance Scenarios**:

1. **Given** a web request missing either identity header, **When** it arrives, **Then** it is
   rejected with a validation error before any handler runs.
2. **Given** identity that reaches the turn empty or malformed, **When** the turn starts, **Then**
   the person is told the system could not verify who is asking and nothing else runs.
3. **Given** any tool that touches tenant data, **When** it is invoked without a valid identity,
   **Then** it refuses on its own, independently of whether an earlier step already checked.
4. **Given** an action name the policy does not recognise, **When** it is checked, **Then** it is
   denied even for a valid identity (unknown ⇒ deny).
5. **Given** a message that says "I am from tenant B" or a tool argument naming a tenant, **When**
   the turn runs, **Then** the identity used remains the one stamped at the boundary.
6. **Given** the structured-data tool exposed to external clients over the tool-protocol server,
   **When** it is called, **Then** it applies the same fail-closed permission check and the same
   mandatory tenant predicate (see *Known gaps* for why the identity there is caller-supplied).

---

### User Story 3 - Personal data stays personal, even inside one organization (Priority: P1)

Within a tenant, some data belongs to a person: their remembered facts and their conversations. A
colleague in the same tenant must not see them. A person's list of past conversations shows only
their own (and only for the assistant/domain they used). Asking to read a conversation that is not
yours is answered exactly as if it did not exist.

**Why this priority**: Tenant isolation alone is too coarse for memories and chat history, which
can contain a person's private context. A second axis nested inside the first prevents one
employee from reading another's remembered preferences or past questions.

**Independent Test**: Two principals in one tenant each create a conversation and a memory. Each
lists conversations and asks questions; confirm each sees only their own, and that requesting the
other's transcript or pending-approval state yields "not found" (404), not "forbidden".

**Acceptance Scenarios**:

1. **Given** two principals in one tenant, **When** each lists conversations, **Then** each sees
   only conversations they started, and only for the domain they ask about.
2. **Given** a conversation id that belongs to someone else (or to nobody), **When** its
   transcript or pending approval is requested, **Then** the response is identical: not found.
3. **Given** a memory written by principal P1, **When** principal P2 in the same tenant asks a
   related question, **Then** P1's memory is not recalled.
4. **Given** the conversation list, **When** the same person switches assistant domain, **Then**
   conversations from the other domain do not appear.

---

### User Story 4 - The assistant remembers across conversations — only when approved, never on its own (Priority: P2)

A person can ask the assistant to remember something ("remember that I prefer short answers").
That is the only way a memory is ever created: an explicit tool call that, like any write, pauses
for human approval before it takes effect. Nothing extracts facts from what a person typed.
Once saved, a memory is recalled automatically in later conversations — the assistant never has to
decide to "go look" — and is presented alongside retrieved documents as numbered, citable material
that is treated as data, not instructions.

**Why this priority**: Cross-session memory is valuable but dangerous: a poisoned document affects
one answer, while a poisoned memory replays on every later turn until removed. Making writes
explicit and approved, and reads automatic and re-filtered, keeps the risk bounded.

**Independent Test**: Ask the assistant to remember a fact; approve the write; start a new
conversation as the same principal and ask a related question — the memory appears as a cited
source. Repeat as another principal — it does not. Never approve — nothing is stored.

**Acceptance Scenarios**:

1. **Given** a request to remember a fact, **When** the assistant calls the memory-write tool,
   **Then** the turn pauses for approval and nothing is stored until it is approved.
2. **Given** a memory content of more than 2,000 characters or only whitespace, **When** the tool
   is called, **Then** it is rejected before any storage.
3. **Given** the model decides, unprompted, that a fact from the conversation is worth keeping,
   **When** the turn ends, **Then** nothing is stored — no code path creates a memory except the
   approved tool call.
4. **Given** a saved memory, **When** a later conversation by the same principal asks something
   related, **Then** it is recalled automatically, numbered with the document sources, and framed
   as untrusted data.
5. **Given** the same write is replayed (a retried call with the same call id), **When** it runs
   twice, **Then** exactly one memory results (feature 003 owns the mechanism).
6. **Given** a person's clearance or identity changes between turns, **When** the next turn
   recalls memories, **Then** the filter uses the *current* identity — nothing is cached.

---

### User Story 5 - Memories expire and can be deleted, with a record that it happened (Priority: P2)

A memory older than the retention period (default 365 days) is invisible at recall time even if it
has not yet been physically removed. An operator can delete memories — a single memory by id, or
everything older than N days — scoped to the tenant, defaulting to the caller's own memories, and
optionally targeting another principal in the same tenant (for example a departed employee's data
request). An ambiguous request (both selectors, or neither) is refused rather than silently
narrowed to "everything". Every attempt, refused or not, leaves a counted audit trace. Deletion is
deliberately *not* something the assistant itself can do.

**Why this priority**: "Right to erasure" and bounded retention are table stakes for storing
personal facts; a delete the model could invoke would itself be a harder trust problem than the
writes the system already gates.

**Independent Test**: Write memories of several ages; confirm an expired one is not recalled
though still stored; delete by id and by age; confirm counts returned, other tenants and
principals untouched, an ambiguous request refused, and the counters and log line recorded.

**Acceptance Scenarios**:

1. **Given** a memory older than the retention period, **When** recall runs, **Then** it is not
   returned, with no sweep required.
2. **Given** both or neither selector is supplied, **When** deletion is requested, **Then** it is
   refused with an error and counted as refused; nothing is deleted.
3. **Given** a valid single selector, **When** deletion runs, **Then** only memories in the
   caller's tenant (and, by default, the caller's own) are removed, and the number removed is
   returned.
4. **Given** any deletion attempt, **When** it completes, **Then** a counter records the outcome
   (deleted or refused) with no tenant or person in its labels, and a structured log line records
   tenant, principal, which selector, and how many.
5. **Given** a missing or malformed identity, **When** deletion is requested, **Then** it is
   refused and counted.

---

### User Story 6 - Writes to the shared knowledge base carry their tenant (Priority: P2)

Content enters the knowledge base through ingestion (uploaded documents, crawled pages, seeded
samples) and through an approved "add a note" tool. Every such record is stamped with the writing
tenant, ingestion also records who ingested it, and content that arrives without a verified
identity is refused outright — never ingested as tenant-less or public content.

**Why this priority**: Reads can only be scoped if writes were scoped first; an unowned record
would be either invisible to everyone or visible to everyone.

**Independent Test**: Attempt ingestion without identity (refused, counted); ingest as tenant A
and confirm a tenant-B search cannot find it; add a note and confirm it carries the tenant.

**Acceptance Scenarios**:

1. **Given** ingestion without a valid identity, **When** it is attempted, **Then** it is refused
   and counted by reason; nothing is stored.
2. **Given** a document ingested by tenant A, **When** tenant B searches for its content, **Then**
   nothing is returned.
3. **Given** a note added through the approved tool, **When** it is stored, **Then** it carries
   the caller's tenant (and is visible tenant-wide — see *Known gaps* on per-person authorization).

---

### Edge Cases

- A memory written before the creation timestamp existed has no timestamp → it is excluded from
  recall as if expired (invisible until re-written), not treated as "never expires".
- Recall runs with a missing or malformed identity → returns nothing (enrichment never fails the
  turn), and refuses the lookup rather than falling back to an unscoped one.
- Searching with a topic and a document-id narrowing at once → both intersect with the tenant
  scope; neither can substitute for it.
- A tenant name containing characters that are syntax in the answer cache's query language (for
  example a hyphen, as in `other-co`) → still matches only itself (escaped).
- A conversation id supplied by the client is *reused by a different tenant* → see *Known gaps* B2;
  this is the case the system does **not** currently stop on the continue/resume/cancel paths.
- Two principals share one tenant's documents but not each other's memories → documents are
  tenant-wide by design; memories are owner-scoped.
- The memory-deletion count and the delete itself are two separate store calls → a memory written
  in between is deleted but not counted (accepted race, disclosed).

## Requirements *(mandatory)*

### Functional Requirements

**Identity**

- **FR-001**: The system MUST obtain the caller's identity (tenant, principal, an opaque claims
  bag) exactly once, at a trusted boundary, and treat it as read-only for the rest of the turn.
  The boundaries are: required identity headers on web requests; the OS user for the CLI; the
  chat-app user id for Telegram; a fixed ingest identity for seeding; and explicit arguments on the
  external tool-protocol server (see *Known gaps*).
- **FR-002**: Identity MUST NOT be derived from message content, request-body fields, tool
  arguments or model output; no model-visible tool schema may expose tenant or principal.
- **FR-003**: A missing, empty or malformed tenant or principal MUST be refused — never defaulted
  to a tenant — at turn entry, and again independently inside every tool that touches tenant data.
  A web request missing either header MUST be rejected before any handler runs.
- **FR-004**: The access policy MUST deny an action it does not recognise and any action for a
  malformed identity, and MUST return a denial rather than raise on bad input.
- **FR-005**: The system MUST state that identity headers are a trusted-layer seam and *not*
  authentication (see *Known gaps*).

**Scoping of reads**

- **FR-006**: Every read MUST be restricted to the caller's tenant by the store's own query
  (a store-native predicate), never by filtering an already-fetched result in application code.
- **FR-007**: For hybrid document search, the tenant predicate MUST be applied inside **each**
  retrieval leg and not only after fusion. Document searches MUST be scoped to tenant *and*
  document kind; memory searches to tenant, memory kind, **owner**, and the retention window.
- **FR-008**: Optional narrowing (topic, department, name substring, specific document ids) MUST
  only intersect with the scope; it MUST NOT be able to widen or replace it.
- **FR-009**: Structured-data access MUST use fixed, parameterized queries that always include the
  tenant predicate; no tool may expose a model-written query, and results MUST be capped per tool
  with a visible truncation marker.
- **FR-010**: A child record MUST carry its own tenant column and MUST be read with its own
  tenant predicate rather than inheriting tenant solely through its parent.
- **FR-011**: The stored-answer cache MUST be scoped to tenant **and** principal, with tenant and
  principal values escaped so that punctuation in either cannot alter the query.
- **FR-012**: A person's conversation list MUST be scoped to tenant, principal and domain.
  Reading a conversation's transcript or pending-approval state MUST first check that the
  conversation belongs to the caller (tenant, principal, domain) and MUST answer an unowned or
  non-existent conversation identically (404). **As built, this check exists only on those two
  read endpoints** — see B2.
- **FR-013**: A tenant's usage and cost figures MUST be scoped to that tenant; no endpoint may
  return another tenant's spend.

**Scoping of writes**

- **FR-014**: Every record written to the shared knowledge base MUST carry the writing tenant;
  ingestion MUST also record the ingesting principal.
- **FR-015**: Ingestion MUST refuse content without a valid identity and MUST NOT ever store
  tenant-less content; each refusal MUST be counted by reason.
- **FR-016**: Content-addressed record ids for ingested content MUST include the tenant, so two
  tenants uploading identical content never collide onto the same record.
- **FR-017**: A write's target identity MUST be derived by code (a hash of tenant, source, position
  and content; or of the tool-call id), never supplied by the model.

**Memory**

- **FR-018**: A memory MUST be created only by the explicit memory-write tool, which MUST be
  declared as a mutating capability and therefore pass the mandatory approval gate (feature 003).
  No code path may extract or write a memory from turn text on its own.
- **FR-019**: A memory's content MUST be non-blank and at most 2,000 characters; its tenant, owner
  and creation timestamp MUST be stamped by the system and never supplied by the caller.
- **FR-020**: Memory recall MUST be automatic (folded into the pre-fetch every turn), MUST apply
  the filter from FR-007 using the *current* identity on every call, and MUST NOT be cached across
  calls. There MUST be no model-invokable tool to read or delete memories.
- **FR-021**: Recalled memories MUST be numbered with the document sources and framed as
  untrusted data, exactly as retrieved documents are.
- **FR-022**: A memory older than the retention period (default 365 days) MUST be excluded at
  recall time regardless of whether any sweep has run; a memory with no creation timestamp MUST
  also be excluded.
- **FR-023**: Memory deletion MUST be an operator capability, not an agent capability; it MUST
  require exactly one selector (a memory id, or an age in days) and refuse both/neither; MUST be
  scoped to the caller's tenant; MUST default to the caller's own memories and MAY target another
  principal within the same tenant; MUST return the number removed.
- **FR-024**: Every deletion attempt, including a refused one, MUST increment a counter by
  outcome (labels carrying no tenant or principal) and MUST write a structured log line carrying
  tenant, principal, selector kind and count. A missing/malformed identity MUST be refused and
  counted.
- **FR-025**: The memory model MUST be written down as one where *writing is opt-in and reading is
  not*: whatever writes memory decides what is replayed into every future prompt, so autonomous
  writes would be a privileged side channel.

### Key Entities *(include if feature involves data)*

- **Tenant**: An organization sharing the deployment. Invisible to every other tenant. A string
  identifier; one principal belongs to exactly one tenant.
- **Principal**: A person (or integration) within a tenant — the *owner* of memories and
  conversations. Stamped from the trusted boundary.
- **Identity (SecurityCtx)**: `{tenant, principal, claims}` stamped once per turn; `claims` is an
  opaque pass-through no code branches on today.
- **Policy**: The pure access model: may this action happen for this identity at all, and what
  store-native predicate expresses "what this identity may see" for documents vs. memories.
- **Document**: Shared knowledge-base content, tenant-scoped, tenant-wide within the tenant.
- **Memory**: A fact a person asked the assistant to keep: owner-scoped, timestamped, expiring,
  deletable.
- **Conversation (Session)**: A person's thread; listed per tenant+principal+domain; its stored
  state is not itself keyed by owner (see B2).
- **Cached Answer**: A stored answer keyed by meaning, tenant and principal.
- **Deletion Audit Record**: A counter increment (outcome only) plus a structured log line.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a two-tenant evaluation, 0% of document searches, structured-data queries,
  cached-answer lookups and conversation-list requests from tenant A return any item belonging to
  tenant B.
- **SC-002**: 100% of requests with missing or malformed identity are refused before any
  retrieval, cache lookup, tool run or model call, and each refusal is counted.
- **SC-003**: A memory written by one person is never recalled for a different person, including
  a different person in the same tenant.
- **SC-004**: A memory past the retention period is never recalled, even when still stored.
- **SC-005**: 100% of memory-deletion attempts — including refused ones — are visible as a counted
  event, and no deletion ever removes a memory outside the caller's tenant.
- **SC-006**: Zero memories are created without an approved explicit write: in a conversation where
  the person never asks to remember anything, the number of stored memories stays zero.
- **SC-007**: Two tenants ingesting byte-identical content end with two independent sets of
  records; neither ingestion changes what the other tenant sees.
- **SC-008**: A request for another person's conversation transcript or pending-approval state
  returns a response indistinguishable from a request for a conversation that does not exist.

## Assumptions & Known Gaps

**Assumptions**

- This feature is *authorization/isolation*, not *authentication*. A trusted gateway in front of
  the service authenticates callers and sets the identity headers, stripping any client-supplied
  copies. The system's job is to fail closed on the *shape* of that contract.
- One physical store is shared by all tenants (payload-filtered, not one store per tenant), so
  isolation is a property of every query, not of the storage layout.
- A tenant name is an opaque string and a principal belongs to exactly one tenant.
- Documents are tenant-wide by design; only memories and conversations are owner-scoped.
- The retention period (default 365 days) is a tunable; the *existence* of retention at recall time
  is the requirement.

**Out of scope for this feature**

- The approval gate itself and exactly-once writes → `003-approval-and-exactly-once-writes`.
- The per-tenant daily budget, rate limiting, and cost ledger (cost governance) and the queue/worker
  transport — not covered by the first spec batch.
- Domain composition (the support / ops / sales stores use the same tenant rules; they are specified
  with domain composition, not here).

**Known gaps (disclosed, with how each was established)**

- **Bug B2 — a conversation id is not ownership-checked when a message is sent, a paused turn is
  resumed, or a turn is cancelled (reproduced at the graph level; endpoint behavior established by
  reading the code).** The ownership check exists only on the transcript and pending-approval read
  endpoints. The send-message, resume and cancel endpoints never call it, and the worker does not
  check either. A conversation's stored state is keyed by the conversation id alone, so whoever
  supplies that id continues that conversation. Reproduced with a fake model and an in-memory
  store: tenant A's conversation was continued by tenant B (different tenant and principal) on the
  same id; tenant A's earlier message was part of the history tenant B's turn ran on, and the
  stored identity switched to tenant B. Consequences, by reading: a caller who knows or guesses
  another's id can read (via the model) that history, and can approve/reject/cancel their pending
  action — an approved action would run under the *resumer's* identity, not the owner's. The id is
  client-supplied (a UUID by default, but any string is accepted, e.g. `qs-1`), so "unguessable" is
  a convention, not a guarantee; and on the chat-app channel the id is not random at all — it is
  derived from the chat id (`telegram:<chat id>`), a small predictable integer, in the default
  shared tenant. Not reproduced end-to-end against a running stack. Not fixed here.
- **Real authentication is not built, and the shipped production proxy does not provide it.** The
  README's Roadmap says so. Reading the production reverse-proxy configuration shipped in the
  repository confirms it forwards straight to the API with no authentication and no handling
  (setting or stripping) of the identity headers, so deployed as shipped, a caller can name any
  tenant and any principal. In that
  configuration isolation protects against *bugs*, not against a caller who sets another tenant's
  header. The demo web UI makes both fields editable by design.
- **No per-person authorization within a tenant.** Every principal in a tenant holds the same write
  capability the approval gate allows at all. The "add a note" tool writes a tenant-wide document
  and does not record which principal wrote it (ingestion does record `ingested_by`).
- **Some data is deliberately not tenant-scoped, and nothing authorizes who may reach it.** The
  operations assistant's incident log is a documented, deployment-wide data set (its source of
  truth is the platform's own metrics, which have no tenant dimension), so identity there proves
  only "a legitimate caller of this deployment" and rows are attributed by reporting principal. The
  assistant "domain" is chosen by a request header and validated only as *existing*; no rule says
  which tenant may use which domain. So whenever an operations worker pool is running, any caller
  who can name that domain can reach its read tools. This is a consequence of the same missing
  per-person/per-domain authorization, recorded as a deliberate exception to "every read is
  tenant-scoped" rather than an accident.
- **Memory deletion has no runnable entry point.** The deletion capability is a library function
  that no script, endpoint or make target calls (only tests do), so an operator cannot actually
  perform an erasure without writing code. Retention is enforced at recall time only; nothing
  physically sweeps expired memories. `target_principal` lets an operator act on another principal
  in the tenant but the function does not itself authenticate that entitlement. Count-then-delete is
  not atomic.
- **Real-backend isolation proof is partial.** Two stores *are* tested against real services, in the
  integration tier's concurrency suite: knowledge-base search across six concurrent tenants on a real
  vector store (each tenant surfaces only its own document), and the answer cache on a real search-enabled
  cache (a second tenant's first ask never hits another tenant's entry; the tenant names there contain a
  hyphen, so it also exercises the escaping fix). **Nothing** tests against a real service: memory scoping
  by owner or retention, the narrowing-by-id rule, the relational stores, the session directory, the
  cache's *principal* axis, or conversation ownership (B2). Those are proven only by filter-construction
  and query-text unit tests (the constitution already records the fake-cursor limitation for relational
  stores). *This bullet originally said no real-backend test existed; that was wrong — my search covered
  only two test directories — and was corrected after `/speckit-analyze`.*
- **The external tool-protocol server takes tenant and principal as caller-supplied arguments**
  (a documented demo simplification; a production server would derive them from the client's
  verified identity). It still applies the policy check and the mandatory tenant predicate.
- **Job and results streams rely on unguessable ids, not ownership checks** (a deliberate, documented
  posture for server-generated ids; it does not extend to the client-supplied conversation id).
- `claims` is carried but no code reads a key from it.
- The deduplication table used for exactly-once writes is looked up by call id alone, not by tenant
  (feature 003 records the assumption this rests on).
- The policy contract states it is pure ("no I/O, no clock"), but the memory filter reads the
  current time to compute the retention cutoff. Harmless for isolation; the contract wording is
  inaccurate.
