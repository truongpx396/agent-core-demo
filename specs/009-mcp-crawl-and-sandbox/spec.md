# Feature Specification: MCP, Web Crawl and Sandbox Integrations

**Feature Branch**: `009-mcp-crawl-and-sandbox`

**Created**: 2026-10-03

**Status**: Implemented (retrospective) — with five reproduced defects, see *Known gaps* B23–B27

**Input**: User description: "Reaching outside the process (retrospective spec of the as-built system): the assistant can run a shell command or a short script in an isolated, disposable environment that belongs to the current conversation; it can read a live, script-rendered web page after the address has passed a check that it points at the public internet; it can use tools published by another program without trusting that program's own claim about whether a tool is safe; and the company data and operations tools can be published to other programs. Every such action that reaches outside needs a human's approval, results are cleaned of credentials, and a dependency that is down degrades the feature rather than the turn."

> **Retrospective note.** Written after the system was built (Spec Kit adopted 2026-10-01) from the code, the compose and server
> configuration, `GRAPH_PATTERNS.md` patterns 21, 28 and 50, the constitution's Principles I, II, V and VI, and the tests named in
> `quickstart.md`. It describes what the system does today. **Boundaries.** The approval gate that makes "outward" mean
> something, and the duplicate-call protection around every write, are feature 003; which tools each product exposes and the
> unbounded-argument gap across all of them are feature 005; the page-fetch entry point used by *ingestion* is feature 006; the circuit breaker's metrics are
> feature 008; the container-image gap that also hides the skill and subagent catalogs is feature 007's B16. This feature is **the three
> doors out of the process**: a code sandbox, a headless-browser page reader, and the Model Context Protocol in both directions.
> Five defects were found by *reproducing* behavior — one inside the real container image, one against the real tool shapes with a
> stand-in sandbox service, and three with a patched crawler, literal addresses or a stand-in DNS call (B23–B27); nine further gaps were found by reading
> the code, configuration and tests. All are in *Known gaps*.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The assistant runs code in a disposable sandbox that belongs to the conversation (Priority: P1)

For work it cannot do in its head — a multi-year pricing schedule, a percentile over a pasted log, a diff of two configurations — the
assistant runs a short script or a shell command in an isolated, throwaway environment. It never sees or chooses an environment
identifier: the first call in a conversation creates one, later calls reuse it, and it disappears on its own after half an hour. Every
such call pauses for a person's approval first. If the sandbox service is down, the assistant is told so plainly and carries on.

**Why this priority**: It is the only way the assistant can compute anything beyond a single arithmetic expression without trusting its own mental maths.

**Independent Test**: In each product, ask for a computation that needs a script; approve the pause; confirm the result, then ask a follow-up that reads a file the first call wrote and confirm it is the same environment.

**Acceptance Scenarios**:

1. **Given** a conversation's first sandbox call, **When** it runs, **Then** one environment is created, tagged with that conversation, with a 30-minute lifetime; **given** later calls, **Then** the same environment is found again by a lookup on the server (so a different worker finds it).
2. **Given** a Python script containing quotes and newlines, **When** it is run, **Then** it reaches the sandbox as a plain file (never through a shell) and runs as a separate step; a single wrapping markdown fence is stripped.
3. **Given** any of the four sandbox tools in any product, **When** the assistant calls it, **Then** the turn pauses for approval; **given** approval, **Then** it runs once.
4. **Given** a result, **When** it is returned, **Then** it reads as an exit code, the standard output and (only when non-empty) the standard error.
5. **Given** the lookup for an existing environment fails, **When** a call is made, **Then** it falls through to creating a new one; **given** creation fails or returns no identifier, **Then** a clear error is raised.
6. **Given** the sandbox service is unreachable, **When** a call is made, **Then** the assistant is told it is "not reachable right now" and the turn continues; the tool catalogue lookup is retried once and then fails fast; **a command or file write is never retried**.
7. **Given** two different conversations, **When** each uses the sandbox, **Then** *(intended)* each reaches only its own environment — in the same or another organization. **As built two distinct conversation ids can resolve to one environment — see B24.**
8. **Given** a containerized deployment, **When** a sandbox tool is called, **Then** *(intended)* it works. **As built it can never work there — see B23.**

---

### User Story 2 - The assistant reads a live web page after the address passes a public-internet check (Priority: P1)

For a lead's company site, a vendor's status page or an external reference, the assistant asks to read a page that needs a real browser to show
its content. The address is checked first — secure scheme, a real host name, every address it resolves to public — and only then is a headless browser asked
to render it. The person approves each read. The page comes back as clean text, capped in size, or the assistant is told why it could not be read.

**Why this priority**: The assistant's answers about leads, vendors and outside documentation are only as good as what it can actually look at.

**Independent Test**: Ask for a page on a public site (approve) and for an address on a private network (refused before any browser starts); confirm the first returns capped text and the second a refusal.

**Acceptance Scenarios**:

1. **Given** any of the three page-reading tools (lead enrichment, external reference, vendor status), **When** it is called, **Then** the turn pauses for approval; none runs unattended.
2. **Given** an address that is not secure-scheme, has no host, does not resolve, or resolves to a loopback, private, link-local, reserved, multicast or unspecified address — or to a *mixture* with one such address — **When** it is submitted, **Then** it is refused before any browser is launched.
3. **Given** an address in the shared carrier-grade address space (100.64.0.0/10), **When** it is submitted, **Then** *(intended)* it is refused. **As built it is accepted — see B26.**
4. **Given** the check runs, **When** name resolution is slow, **Then** *(intended)* other work is unaffected. **As built it stalls the whole process — see B27.**
5. **Given** a rendered page longer than 20,000 characters, **When** it is returned, **Then** it is cut and marked as truncated.
6. **Given** the browser service is unreachable, a navigation fails or the page has no content, **When** the read ends, **Then** the assistant gets one short line — never the server's multi-line internals.
7. **Given** a bare connection failure to the browser service, **When** a read is attempted, **Then** it is retried (3 attempts) and, after three consecutive exhausted failures, further reads fail fast for 30 s; a request the service answered with an error is **not** retried.
8. **Given** lead enrichment, **When** it runs, **Then** it requires an existing lead, saves the page text as **one** note keyed to the call (so a replay does not duplicate it), and returns only a short summary.
9. **Given** a page that redirects or navigates to a non-public destination, **When** it is read, **Then** *(intended)* it is refused. **As built the check applies to the first address only — see A1.**

---

### User Story 3 - What the outside world returns is treated as data, cleaned and bounded (Priority: P1)

Whatever a sandbox prints, a web page says or another program's tool returns arrives as *data*. It is cleaned of anything that looks like a credential before
it can reach a prompt or a trace; a tool published by another program is *never* trusted to say whether it is safe; and web content that came from a stranger is marked as
such wherever it re-enters the assistant's context.

**Why this priority**: These are the three highest-risk sources of attacker-controlled text and the three places an injected instruction would try to become an action.

**Independent Test**: Return a credential-shaped string from a fake remote tool; confirm it is masked. Register a remote tool that claims to be read-only; confirm it still needs approval. Read a page containing an instruction; confirm the instruction is delimited as untrusted wherever it is shown to the model.

**Acceptance Scenarios**:

1. **Given** output from a remote tool, a sandbox or a crawl, **When** it is returned, **Then** credential-shaped values are masked.
2. **Given** a remote tool that declares itself read-only, **When** it is loaded, **Then** its capability is whatever the *local* caller names, defaulting to "outward" — the remote's own hints are ignored.
3. **Given** the sandbox bridge's tools, **When** they are loaded, **Then** every one is outward (no overrides are ever supplied).
4. **Given** a crawled page, **When** it is returned to the model **or replayed later from stored notes**, **Then** *(intended)* it is delimited as untrusted data and the replay is bounded. **As built it is neither — see B25.**
5. **Given** a sandbox command, a script or a file's content, **When** it is large, **Then** *(intended)* arguments and returned output are length-bounded. **As built they are not — see A3.**

---

### User Story 4 - Tools from another program can be used, and this app's read-only data tools can be published (Priority: P2)

The assistant can use a tool catalogue published by another program over the standard protocol (today, the sandbox bridge); and the employee
directory and the operations metrics and incidents can be published the other way, so an engineer's desktop assistant can query them directly.

**Why this priority**: It keeps integrations out of the graph and lets other agents reach the same governed data.

**Independent Test**: Load a fake remote catalogue and call one tool; start each published server and call its tools with and without an identity.

**Acceptance Scenarios**:

1. **Given** a remote catalogue, **When** it is loaded, **Then** each tool is wrapped with its own parameter schema so the assistant sees real parameter names, and a remote error comes back as text, not an exception.
2. **Given** each tool call, **When** it runs, **Then** a fresh connection is opened and closed (no persistent session).
3. **Given** a call to a published server with no tenant or principal (or no principal, for the operations server), **When** it arrives, **Then** it is refused before any data is touched; **given** an identity, **Then** a fixed, parameterized query runs scoped to it.
4. **Given** the directory server and the operations server, **When** they run, **Then** they are separate processes (one per domain, like the workers).
5. **Given** a connecting program, **When** it names a tenant, **Then** *(intended)* the tenant comes from the program's verified identity. **As built the caller supplies it, unauthenticated — see A4 (disclosed in the code).**

---

### User Story 5 - A dependency that is down degrades the feature, never the turn (Priority: P2)

The sandbox service and the browser service are separate containers that can be briefly down while starting or restarting. When one is, the product still starts,
every other tool works, and the affected tool says what is wrong. Short outages ride out; long ones fail fast without each call paying a full connection timeout.

**Why this priority**: Constitution Principle V — a dependency failure picks a policy deliberately and every wait is bounded.

**Independent Test**: Start the application with both services stopped; confirm it builds and answers, and that the two tools report the outage within their timeouts.

**Acceptance Scenarios**:

1. **Given** the sandbox bridge is missing or unreachable, **When** the catalogue is loaded, **Then** it degrades to "no tools" with a logged warning and **never raises**, and an empty result is **not** cached so a later call retries.
2. **Given** a connection-shaped failure, **When** it happens, **Then** it is retried (sandbox listing: once after 1 s; page reads: twice, from 0.5 s) with jittered back-off; a non-connection failure is never retried.
3. **Given** a breaker that has opened, **When** its cooldown elapses, **Then** exactly one trial call is admitted and its outcome closes or re-opens it; every other caller meanwhile is rejected as if still open.
4. **Given** each retry and breaker transition, **When** it happens, **Then** it is counted per dependency.

---

### Edge Cases

- A sandbox's contents vanish when its 30-minute life ends; the next call silently creates a new, empty one.
- Reading a file back immediately after writing it fails against the pinned third-party sandbox release (a known upstream bug); each product's command tool tells the model to use `cat` instead.
- The conversation tag must be 63 characters or fewer, start and end alphanumeric and use only letters, digits, `-`, `_` and `.`; real conversation ids contain other characters (a colon in the chat channel's) and are *rewritten* to fit — see B24.
- The browser service is secure-by-default: without its bearer token it refuses everything; the app sends no browser configuration at all (the server rejects any it considers untrusted).
- The sandbox bridge is a child process spawned on every catalogue load and every remote call; its API key is passed as a command-line argument.
- The production compose file defines neither service, only the environment variables to reach them; blank values disable the features.
- `write_sandbox_file` and the command tools are idempotent through the standard call-id protection (feature 003); the sandbox itself is not.

## Requirements *(mandatory)*

### Functional Requirements

**Sandbox**

- **FR-001**: Each product MUST expose four sandbox tools — run a shell command, run a Python script, read a file, write a file — each taking only flat string arguments; the assistant MUST never see or supply a sandbox identifier.
- **FR-002**: The first sandbox call in a conversation MUST create one sandbox tagged for that conversation with a configurable lifetime (default 30 minutes); later calls MUST find and reuse it through a lookup on the sandbox service, not process memory.
- **FR-003**: A failing lookup MUST fall through to creating a sandbox; a failing creation, or one returning no identifier, MUST raise a clear error.
- **FR-004**: Every sandbox tool MUST be declared outward and therefore pause for approval in every product; none MAY be reachable from a scheduled or unattended path.
- **FR-005**: A Python script MUST reach the sandbox as a file written as plain text and run as a separate step, never through a shell; a single wrapping markdown fence MUST be stripped and any other triple-backtick MUST be left alone.
- **FR-006**: A result MUST be rendered as exit code, standard output and standard error (the last only when non-empty); a remote error, an unparseable reply or a non-object reply MUST raise a typed failure.
- **FR-007**: A command or a file write MUST NOT be retried automatically; only the read-only catalogue lookup MAY be.
- **FR-008**: A catalogue that cannot be loaded MUST degrade the tools to a clear "not reachable right now" message, MUST NOT stop a product from being built, and an empty result MUST NOT be cached.
- **FR-009**: A conversation's sandbox MUST be reachable from that conversation only — never from another conversation, principal or tenant. *(Not met — B24.)*
- **FR-010**: The sandbox tools MUST work in every supported deployment mode, including containers. *(Not met — B23.)*
- **FR-011**: The free-form arguments and the returned output of the sandbox tools MUST be length-bounded. *(Not met — A3.)*

**Web page reading**

- **FR-012**: The three page-reading tools MUST be declared outward and MUST NOT be reachable from an unattended path.
- **FR-013**: A submitted address MUST be secure-scheme and have a host, and every address it resolves to MUST be public (loopback, private, link-local, reserved, multicast and unspecified refused); one non-public address in a mixed set MUST refuse the whole; refusal MUST precede any browser launch.
- **FR-014**: Every non-public range, including the shared carrier-grade space (100.64.0.0/10), MUST be refused. *(Not met — B26.)*
- **FR-015**: The address check MUST NOT block other work in the process. *(Not met — B27.)*
- **FR-016**: A redirect, in-page navigation or subresource request that leaves the validated address MUST NOT be followed to a non-public destination. *(Not met — A1.)*
- **FR-017**: A page read MUST be bounded — 30 s page timeout, 40 s overall — and the returned text MUST be cut at 20,000 characters with a marker; a failed navigation MUST be reported as its first line only.
- **FR-018**: A bare connection failure to the browser service MUST be retried (3 attempts, from 0.5 s) behind a circuit breaker (3 consecutive exhausted failures, 30 s cooldown, one trial at half-open); a request the service answered with an error, and a failed render, MUST NOT be retried.
- **FR-019**: Lead enrichment MUST require an existing lead, MUST save the page text as one note keyed by the call id, and MUST return only a short summary.

**Untrusted content**

- **FR-020**: Every result from a sandbox, a remote tool or a page read MUST be credential-scrubbed before reaching a prompt or a trace.
- **FR-021**: A remote tool's capability MUST come only from the local caller and default to outward; the remote server's own annotations and descriptions MUST NOT decide whether approval is needed.
- **FR-022**: Web content MUST be delimited as untrusted data wherever it re-enters the assistant's context — in the tool result and when replayed from stored notes — and a replay MUST be bounded. *(Not met — B25.)*

**Model Context Protocol**

- **FR-023**: The client MUST connect over standard input and output, open one connection per call, wrap each remote tool with its own parameter schema, and return a remote error as text rather than raising.
- **FR-024**: Each published server MUST refuse a missing identity before touching data and MUST run a fixed, parameterized query; the directory and operations servers MUST be separate processes.
- **FR-025**: A published server MUST derive identity from the connecting program's verified identity, never from a caller-supplied argument. *(Not met — A4; disclosed.)*
- **FR-026**: A remote tool's text MUST NOT be trusted as instructions and the bridge's credential MUST NOT appear on a command line. *(Not met — A5.)*

**Resilience and operation**

- **FR-027**: Both external services MUST be optional; each MUST have its own breaker whose retries and transitions are counted per dependency.
- **FR-028**: The sandbox path and the address check's range coverage MUST be exercised by automated tests in the continuous-integration tier. *(Not met — A7.)*
- **FR-029**: Every tunable behind these integrations MUST have an entry in the example environment file. *(Not met — A8.)*

### Key Entities *(include if feature involves data)*

- **Sandbox**: A disposable container, tagged with a rewritten conversation id, living 30 minutes, holding files the assistant wrote.
- **Conversation Tag**: The sanitized conversation id used as the sandbox's lookup key (≤ 63 characters, restricted alphabet).
- **Sandbox Catalogue**: The remote tool list of the sandbox service (about 19 tools), never shown to the assistant — the four flat tools wrap it.
- **Page Read**: One bounded, guarded render of a public address to text.
- **Lead Note**: A saved row of research text on a lead, written by enrichment and replayed by the lead brief.
- **Remote Tool**: A tool published by another program, wrapped with its schema; capability decided locally.
- **Published Server**: A separate process offering the directory or the operations tools to other programs.
- **Circuit Breaker**: Per-dependency retry-and-cooldown state, in process memory.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In every product, 100% of sandbox, page-reading and enrichment calls pause for approval; 0 run unattended.
- **SC-002**: A command or file write is dispatched at most once per approved call: 0 automatic retries.
- **SC-003**: 0 results from these tools contain a credential-shaped value that was in the raw output.
- **SC-004**: A remote tool the local caller did not name is treated as outward 100% of the time, whatever it claims.
- **SC-005**: Every address that is not secure-scheme, has no host or resolves to any non-public range is refused before a browser starts. *(Not met — B26: the shared carrier-grade range is accepted.)*
- **SC-006**: A conversation can reach only its own sandbox. *(Not met — B24: `telegram:12345` and `telegram-12345` shared one environment.)*
- **SC-007**: With either service down, a turn still completes and the tool reports it within its timeout; with the service up, every deployment mode can use the sandbox. *(Second half not met — B23: 0 tools in the container image.)*
- **SC-008**: A page read returns at most 20,000 characters plus a marker; no web content re-enters the assistant's context unframed or unbounded. *(Second half not met — B25: a 59,150-character brief with the injected line verbatim.)*
- **SC-009**: The address check never stalls unrelated work in the process. *(Not met — B27: a 0.51 s stall for a 0.5 s resolution.)*
- **SC-010**: The sandbox path and the address check's ranges are covered by CI. *(Not met — A7.)*

## Assumptions & Known Gaps

**Assumptions**

- A code sandbox is an isolated, disposable container managed by a third-party service; its isolation guarantees are that service's, configured here.
- The assistant's own human-approval gate is the safety boundary for anything that reaches outside; the three doors add scrubbing and bounds, not a second gate.
- A page read is read-only: retrying a failed connection can duplicate nothing.
- The published servers are run locally over standard input and output by the person using them; a networked, authenticated transport is not built.

**Out of scope for this feature**

- The approval gate, mandatory capability declaration and call-id protection (feature 003); the tool arguments' length bounds in general (feature 005's A7); the ingestion page fetch (feature 006); the breaker's metrics and alerts (feature 008).
- A tenant-isolated sandbox pool, per-tenant quotas, OCR or screenshot tools, an authenticated MCP transport, a persistent MCP session.

**Known gaps (disclosed, with how each was established)**

- **Bug B23 — the sandbox tools can never work in the container images (reproduced in the real image).** The sandbox tools reach the sandbox service through a small bridge program that the application spawns from `scripts/opensandbox_mcp_bridge.py`; the Dockerfile copies only `app/`. In the locally built API image: `/app/scripts/opensandbox_mcp_bridge.py` does not exist; the sandbox and browser libraries *are* installed; and running the catalogue loader inside the image (with the compose-style service address) printed `python: can't open file '/app/scripts/opensandbox_mcp_bridge.py'`, logged `opensandbox_mcp_unavailable` and returned **0 tools** in 0.0 s. Every sandbox call in a containerized deployment therefore answers "OpenSandbox is not reachable right now … may still be starting" for ever, while the compose file points the app at the sandbox service. Same root cause as feature 007's B16 (a Dockerfile written before the folders it needs); the sandbox compose services arrived 2026-09-08, after it. Nothing notices: the sandbox tests are a manual tier and the live tier runs host-native. Not fixed here.
- **Bug B24 — two conversations can share one sandbox, including across organizations (reproduced against a stand-in service with the real tool shapes).** The sandbox is found by a lookup on one tag: the conversation id **rewritten** to fit the sandbox service's rules (every character outside letters, digits, `-`, `_`, `.` becomes `-`; truncated to 63; edge punctuation trimmed). The tag carries no organization, person or hash. Reproduced with the real session code against a fake sandbox service that filters by tag the way the real one's documented response does: a file written under conversation `telegram:12345` was **read back** under `telegram-12345` (1 environment created for 2 ids), and two 64-character ids differing only in the last character resolved to one environment. The conversation-ownership check (feature 002) compares the *raw* id, so it does not stop this; the reserved-prefix guard for the chat channel's ids only matches the colon form. Any web client may choose any conversation id (the request default is a random id but any string is accepted). The code calls such collisions "theoretical at this app's scale"; a person who can pick an id can make one on purpose — and the chat channel's ids are guessable numbers. The sandbox holds whatever the assistant wrote into it, which may be one organization's data. The real sandbox service's lookup was not exercised. Not fixed here.
- **Bug B25 — crawled web text is returned, stored and replayed unframed, and the replay is unbounded (reproduced at function level with a patched crawler).** The system prompt treats text wrapped in `<retrieved_document>` tags as untrusted, "whether pre-fetched or returned by a tool call" — but none of the three page-reading tools wraps anything. With the crawler patched to return a page containing an injected instruction: the external-reference tool returned 19,659 characters, **unframed**, with the instruction verbatim; lead enrichment stored a 19,699-character note; and after three enrichments the read-only lead-brief tool returned **59,150 characters, unframed, with the instruction replayed verbatim** — to the model, with no approval (it is read-only), and to the sales product's lead-research specialist (feature 007), which also has it. So a hostile page that a person approved reading becomes a *persistent* instruction in the lead's record, re-injected whenever the brief is built. The approval shows the address, not the page; the human gate does not cover what is *stored*. Constitution Principle VI requires retrieved content to be framed. Not fixed here.
- **Bug B26 — the address check accepts the shared carrier-grade range (reproduced with literal addresses; no network used).** `100.64.0.0/10` is neither "private" nor "reserved" by the standard library's definitions, so `https://100.64.0.1/` and `https://100.100.100.200/` (a cloud provider's metadata address) pass. Checked in the same run: loopback, `::1`, IPv4-mapped loopback and private, the link-local metadata address, NAT64 and 6to4 forms of loopback and metadata, unique-local and link-local IPv6, the integer and hexadecimal spellings of loopback and `192.0.0.192` were all refused; `8.8.8.8` was allowed as it should be. A "global address only" test would close the range. The address ranges also appear in some clusters' pod networks and in overlay networks. Not fixed here.
- **Bug B27 — the address check blocks the whole process while it resolves a name (reproduced).** `assert_safe_url` calls the blocking name-resolution function directly; the page reader awaits nothing before it. Reproduced by replacing resolution with a 0.5 s blocking stand-in: a 10 ms heartbeat saw a **0.51 s** stall during one guarded read. Every concurrent turn on that worker waits out a slow resolver. The ingestion fetch (feature 006; no production caller today) makes the same direct call, so the defect is in the shared guard's use, not in one caller. Not fixed here.
- **A1 — the address check validates the first address once.** Nothing in this repository stops a redirect, an in-page navigation or a subresource from leaving the validated address; the browser service resolves the name again itself (the code discloses the rebinding case); and the browser service sits on the default compose network with the databases, the queue, the vector store, object storage and the model proxy — several of which publish plain-HTTP interfaces. The ingestion fetch knows this risk and refuses to follow redirects for exactly this reason; a headless browser follows them by nature, and nothing here limits it. A page the person approved by *address* could redirect the browser to one of them and have its content returned to the assistant — and, through lead enrichment, saved. *(Read; not exercised: that needs the browser service and a hostile page.)*
- **A2 — the sandbox's posture is looser than the comments say.** The server configuration uses bridge networking with host-published port allocation, sets a process-count limit but no memory or CPU limit, allows sandboxes up to 24 h (the app asks for 30 min), has no per-organization or per-conversation cap, and the control-plane container holds the host's container-runtime socket read-write (disclosed, opt-in profile). The configuration comment and the tool descriptions say a sandbox has no network egress; that rests on a third-party default and an "egress" component whose behavior was **not verified** here (the API key is in a blocked file). A sandbox's state vanishes silently at its lifetime's end. *(Read.)*
- **A3 — sandbox arguments and every result are unbounded.** `command`, `script`, `path` and `content` carry only a non-blank check (feature 005's A7 covers free-form arguments in general); the result is returned whole — a 1 MB standard output came back as a 1,000,021-character string — as is a file read and a remote tool's text. Large outputs go straight into the model's context and its cost. *(Read; the result size checked with a one-line call.)*
- **A4 — the published servers trust caller-supplied identity (disclosed in the code).** The directory server takes `tenant` and `principal` as arguments and the operations server takes `principal`; there is no authentication. Over standard input and output the caller is the local person who started it, so this matches a local tool; any networked transport would be an unauthenticated multi-tenant API. *(Read.)*
- **A5 — a remote tool's text passes through, and the bridge's key is on a command line.** `load_remote_tools` takes the remote tool's name, description and schema as given (only the capability is overridden); the only caller is the local sandbox bridge, so exposure is limited today. The sandbox API key is passed as `--api-key <value>` to the child process and is visible in a process listing; a test asserts it is passed. *(Read.)*
- **A6 — a tool known to fail is still offered.** The pinned third-party release reliably fails to read back a file just written; each product's command tool tells the model to use `cat` instead, but `read_sandbox_file` stays bound. *(Read: the module comment and docstrings.)*
- **A7 — the sandbox path is not in CI and the address check's tests are four cases.** The sandbox marker is a manual tier (`make test-sandbox`); the live tier starts services host-native; the address check's tests cover a non-secure scheme, a private address, loopback and a mixed set — none covers IPv6, link-local, the shared carrier-grade range or mapped forms. *(Read: the workflow, the Makefile and the tests.)*
- **A8 — configuration and comment drift.** `CRAWL4AI_SERVER_URL`, `OPENSANDBOX_MCP_DOMAIN`, `SANDBOX_IMAGE` and `SANDBOX_TTL_SECONDS` have no example-environment entry (the other two do); the dependency comments cite `app/mcp_server.py` and `app/mcp_client.py`, which are now `app/mcp/server.py` and `app/mcp/client.py`; and "no network access" appears in four tool descriptions without a test. *(Read.)*
- **A9 — a permanently broken bridge is silent: not retried, not counted by the breaker, not counted by any metric.** A connection-shaped failure is retried and counted; a failure of another kind — such as the bridge script missing (B23) or crashing at start — is by design not retried and (per the breaker's own test) "does not count against the breaker", so the breaker never opens and every sandbox call respawns the failing child process and logs one `opensandbox_mcp_unavailable` warning. No counter records "sandbox unavailable", and no alert could fire (Principle V requires a counter on every degrade-and-continue path). In the image the whole degrade took 0.0 s, so it is cheap — and invisible. *(Read: the loader, the breaker's tests; reproduced in the image as part of B23.)*
