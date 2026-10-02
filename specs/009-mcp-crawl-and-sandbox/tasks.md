---

description: "Task list for feature 009 — MCP, Web Crawl and Sandbox Integrations (retrospective)"
---

# Tasks: MCP, Web Crawl and Sandbox Integrations

**Input**: Design documents from `/specs/009-mcp-crawl-and-sandbox/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (all present)

**Tests**: INCLUDED. Principle VII requires a regression test for every bug fix. All five defects (B23–B27) were *missed by the existing tests*: the session tests use a fake service and never ask whether two ids reach one box,
the address check has four cases, nothing frames or bounds web text, and nothing inspects the image or runs the sandbox tier in CI. The open test tasks below are the most valuable work in this file.

**Organization**: Grouped by user story so each can be implemented and verified independently.

## Reading this file (retrospective conventions)

- **`[x]`** = built and present in the repository on 2026-10-03; the path is where it lives. Nothing `[x]` needs doing.
- **`[ ]`** = a **disclosed gap that is not built**. Where it fixes a defect the **failing test is written first** (CLAUDE.md working rules): write it, watch it fail on current code, then fix.
- Open ids (see plan.md *Complexity Tracking* / research.md *Deferred*): **B23** no bridge script in the images · **B24** conversation ids that collide after rewriting share a sandbox · **B25** crawled text is returned, stored and replayed
  unframed and unbounded · **B26** the address check accepts 100.64.0.0/10 · **B27** the address check blocks the event loop · **A1** the address check validates one address once · **A2** sandbox posture · **A3** unbounded sandbox arguments and
  output · **A4** published servers trust caller-supplied identity · **A5** remote text passes through and the key is on a command line · **A6** a known-failing read tool is offered · **A7** no CI for the sandbox tier; four address cases ·
  **A8** configuration and comment drift · **A9** a broken bridge is silent.
- **B24 breaks Principle I (NON-NEGOTIABLE) in intent**: a boundary around possibly tenant-owned data keyed by a rewritable string. **Fix it before any other task in this file.** B25 and B26 break Principle VI; B27 breaks Principle V; B23 makes a feature absent
  in containers. Approval (Principle II) holds everywhere it is asserted.
- Tasks needing Docker say `docker`; tasks needing the sandbox service's key say `key` — the key is in a blocked file, so those are for a person to run. Paths are repo-relative. A path that does not exist yet is a file the task creates.

## Format: `[ID] [P?] [Story] Description *(requirements it serves)*`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: US1…US5 from spec.md; Setup / Foundational / Polish carry no story label

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 [P] Settings `crawl4ai_server_url`, `crawl4ai_api_token`, `opensandbox_mcp_domain`, `opensandbox_api_key`, `sandbox_image`, `sandbox_ttl_seconds` in `app/core/config.py` (**four have no `.env.example` entry — A8**) *(FR-002, FR-029)*
- [x] T002 [P] The compose services `crawl4ai` and `opensandbox-server`, the sandbox server's image and configuration, the sandbox image, and the `sandbox-up`, `sandbox-build`, `test-sandbox`, `mcp-serve`, `mcp-serve-ops` targets — in `docker-compose.yml`, `docker/opensandbox-server.Dockerfile`, `docker/opensandbox-server.toml`, `docker/sandbox.Dockerfile`, `Makefile` *(FR-002, FR-027)*
- [x] T003 [P] The pinned dependencies — `mcp[cli]==1.29.0`, `crawl4ai==0.9.3`, `opensandbox-mcp==0.1.1` — in `requirements.txt` and `requirements-lock.txt` *(FR-023)*
- [x] T004 [P] The bridge that forces `use_server_proxy=True` in `scripts/opensandbox_mcp_bridge.py` (**not in any image — B23**) *(FR-002)*

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The pieces every story uses.

- [x] T005 [P] The retry-with-backoff and circuit breaker (retry only named classes, per-dependency state, one half-open trial under a lock) in `app/core/resilience.py` *(FR-018, FR-027)*
- [x] T006 [P] The shared address check `assert_safe_url` and `UnsafeURLError` in `app/core/url_safety.py` *(FR-013)*
- [x] T007 [P] The MCP client — `load_remote_tools`, `_call_remote_tool`, `_wrap_remote_tool` (capability from the caller only, scrubbed replies, errors as text) — in `app/mcp/client.py` *(FR-021, FR-023)*
- [x] T008 [P] The breaker and retry counters `agent_circuit_breaker_{opened,rejected,half_open}_total` and `agent_tool_retry_total` in `app/core/metrics.py` (on no dashboard or alert — feature 008 A4) *(FR-027)*

**Checkpoint**: Foundation ready — a breaker, an address check and a client exist.

---

## Phase 3: User Story 1 — The assistant runs code in a disposable sandbox that belongs to the conversation (Priority: P1) 🎯 MVP

**Goal**: Four flat, approval-gated tools over a per-conversation sandbox.

**Independent Test**: Ask for a computation needing a script; approve; then read back a file the first call wrote — the same sandbox.

### Tests for User Story 1

- [x] T009 [P] [US1] Find, create, reuse, fall through on a failed lookup, raise on a failed create or a missing id; result formatting; script-as-file; fence stripping; file tools; raw-call errors — in `tests/domains/test_sandbox_session.py` *(FR-002, FR-003, FR-005, FR-006)*
- [x] T010 [P] [US1] Bridge arguments, no overrides, degradation, one retry then success, no retry of a non-connection failure, the breaker — in `tests/domains/test_sandbox_tools.py` *(FR-007, FR-008, FR-027, SC-002)*
- [x] T011 [P] [US1] The four tools present as a fixed set, every one outward, each pausing for approval and running once approved — in `tests/domains/support/test_domain.py`, `tests/domains/ops/test_domain.py`, `tests/domains/sales/test_domain.py` *(FR-001, FR-004, SC-001)*
- [x] T012 [P] [US1] The real sandbox service end to end (manual tier, `key`) — in `tests/live/test_sandbox_session_live.py`, `tests/live/test_opensandbox_mcp_live.py` *(FR-002)*

### Implementation for User Story 1

- [x] T013 [US1] The session logic — the tag rewrite, find-or-create, `run_command`, `run_python`, `read`, `write`, `_format_execution`, `_strip_markdown_fence` — in `app/domains/sandbox_session.py` *(FR-001, FR-002, FR-003, FR-005, FR-006)*
- [x] T014 [US1] `load_sandbox_tools` — spawn the bridge, no overrides, a 10 s timeout, one retry, a breaker, degrade to `([], {})`; and the success-only catalogue cache in `sandbox_session.py` — in `app/domains/sandbox_tools.py` *(FR-007, FR-008, SC-002)*
- [x] T015 [US1] The per-product wrappers — args schemas, outward declarations, the call-id protection, the 60 s timeout, `_SANDBOX_TOOLS` — in `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/tools.py` *(FR-001, FR-004)*

### Open follow-ups for User Story 1 (not built) — **B24, B23, A3, A6, A2, A9**

- [ ] T016 [US1] **B24 — write the failing test first**: new `tests/domains/test_sandbox_isolation.py` using a stand-in service that filters by metadata (as in `tests/domains/test_sandbox_session.py`'s `_FakeTool`), asserting that `telegram:12345` and `telegram-12345`, `demo:1` / `demo-1` / `demo 1`, and two 64-character ids differing in the last character each get **distinct** sandboxes, and that the same raw id under two tenants gets two. Fails today (reproduced — quickstart *Scenario B24*) *(FR-009, SC-006)*
- [ ] T017 [US1] **B24 — fix** in `app/domains/sandbox_session.py` and the three wrappers' call sites: tag the sandbox with **two** keys — a hash of the tenant and a hash of the raw conversation id (each ≤ 63 characters, restricted alphabet) — match **both** in `sandbox_list`, and verify the found sandbox's tags before reuse; pass the tenant from the identity (the wrappers already have it), never from an argument *(FR-009)*
- [ ] T018 [US1] **B23 — write the failing test first**: in `tests/test_dockerfile_bridge.py` parse the repo `Dockerfile` and assert it copies `scripts/opensandbox_mcp_bridge.py` (or `scripts/`) into the image. Fails today (`COPY app/` only — reproduced in the real image, quickstart *Scenario B23*). Coordinate with feature 007's T052 and feature 006's `scripts/` use so the Dockerfile is edited once *(FR-010, SC-007)*
- [ ] T019 [US1] **B23 — fix**: copy the bridge into the image in `Dockerfile`; decide with 006 and 007 whether all of `scripts/` ships *(FR-010)*
- [ ] T020 [US1] **B23 — image smoke (`docker`)**: in the `docker-build` job of `.github/workflows/ci.yml`, after the build, run the image and assert `os.path.exists(sandbox_tools._BRIDGE_SCRIPT)` — the check a text test of the Dockerfile cannot make *(SC-007)*
- [ ] T021 [US1] **A9 — write the failing test first**: in `tests/domains/test_sandbox_tools.py` assert that a catalogue load that fails with a non-retryable error increments `agent_sandbox_unavailable_total{reason}`; fails today (log line only) *(FR-027, SC-007)*
- [ ] T022 [US1] **A9 — fix**: add `agent_sandbox_unavailable_total` to `app/core/metrics.py`, increment it in `load_sandbox_tools` in `app/domains/sandbox_tools.py`, and add a rule on it (non-zero for 15 minutes) in `observability/prometheus/alerts.yml` — keeping the no-retry rule for non-connection failures *(FR-027)*
- [ ] T023 [US1] **A3 — write the failing tests first**: in `tests/domains/test_sandbox_session.py` and each product's tests assert the four tools' arguments reject over-long values and that `_format_execution`, a file read and a remote reply are capped with a truncation marker. Fail today (reproduced — quickstart *Check — A3*: 1 MB → 1,000,021 characters) *(FR-011)*
- [ ] T024 [US1] **A3 — fix**: named `max_length` constants on `command`, `script`, `path`, `content` in `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/tools.py` (a single shared constant in `app/domains/sandbox_session.py`, not three), and an output cap reusing the page reader's 20,000 characters in `_format_execution` and `read_sandbox_file_impl` *(FR-011)*
- [ ] T025 [US1] **A6 — decide**: implement `read_sandbox_file` as `cat` internally in `app/domains/sandbox_session.py`, or drop the tool from each product's `_SANDBOX_TOOLS` until the pinned release is fixed; test first either way *(A6)*
- [ ] T026 [US1] **A2 — decide and configure (`key`)**: set memory and CPU limits and an explicit network policy in `docker/opensandbox-server.toml`, add a per-tenant sandbox cap, and **verify and test** the "no egress by default" claim against a real sandbox (a command that tries to reach an internal service must fail); correct `app/core/config.py`'s comment and the four tool docstrings to what was verified *(FR-011, A2)*

**Checkpoint**: US1 is *verified* only after T016–T017 (isolation) and T018–T020 (containers).

---

## Phase 4: User Story 2 — The assistant reads a live web page after the address passes a public-internet check (Priority: P1)

**Goal**: Three approval-gated page readers behind one guarded, bounded render.

**Independent Test**: Read a public page (approve) and a private address (refused before any browser starts).

### Tests for User Story 2

- [x] T027 [P] [US2] Refusal before any crawl, the cap and marker, success, a failed render, a missing error message, an unreachable and a rejecting server, a retried connection failure, no retry of a request error, the breaker — in `tests/ingestion/test_web_crawler.py` *(FR-013, FR-017, FR-018)*
- [x] T028 [P] [US2] The address check — non-secure scheme, private, loopback, a mixed set — in `tests/ingestion/test_ingestor.py` (`TestAssertSafeUrl`) *(FR-013)*
- [x] T029 [P] [US2] The three readers pause for approval as outward tools and run once approved — in `tests/domains/support/test_domain.py`, `tests/domains/sales/test_domain.py`, `tests/domains/ops/test_domain.py` *(FR-012, SC-001)*
- [x] T030 [P] [US2] A real page through a real browser container (`docker`) — in `tests/integration/test_web_crawler_live.py`, `tests/live/test_domain_crawl_tools_live.py` *(FR-017)*

### Implementation for User Story 2

- [x] T031 [US2] `render_url_to_markdown`, `_crawl` (retry, breaker, one-line failures), `CrawlFailed`, the 20,000-character cap — in `app/ingestion/web_crawler.py` *(FR-013, FR-017, FR-018)*
- [x] T032 [P] [US2] The three reader tools and, for enrichment, the one-note-per-call write — in `app/domains/sales/tools.py`, `app/domains/support/tools.py`, `app/domains/ops/tools.py`, `app/domains/sales/store.py` *(FR-012, FR-019)*

### Open follow-ups for User Story 2 (not built) — **B26, B27, A1, A7 (ranges)**

- [ ] T033 [US2] **B26 — write the failing tests first**: new `tests/core/test_url_safety.py` asserting `assert_safe_url` refuses `https://100.64.0.1/` and `https://100.100.100.200/` and still refuses the 13 other probe addresses of data-model §6 (IPv4 and IPv6 loopback, mapped, link-local, NAT64, 6to4, unique-local, integer and hex spellings) and allows a public one. Fails today for the two 100.x addresses (reproduced — *Scenario B26*) *(FR-014, SC-005, SC-010)*
- [ ] T034 [US2] **B26 — fix**: accept only addresses where `ip.is_global` is true (and map IPv4-mapped IPv6 through `ipv4_mapped` first) in `app/core/url_safety.py` *(FR-014)*
- [ ] T035 [US2] **B27 — write the failing test first**: in `tests/core/test_url_safety.py` assert that `assert_safe_url` called from async code does not stall a concurrent heartbeat when name resolution is slow (a patched resolver sleeping 0.5 s). Fails today (reproduced — *Scenario B27*: 0.51 s) *(FR-015, SC-009)*
- [ ] T036 [US2] **B27 — fix**: add an async entry point that resolves with `asyncio.to_thread` (or the loop's resolver) in `app/core/url_safety.py` and use it from `app/ingestion/web_crawler.py::render_url_to_markdown` and `app/ingestion/ingestor.py::ingest_url`; keep the sync function for sync callers *(FR-015)*
- [ ] T037 [US2] **A1 — decide and implement**: (a) put the browser container on a network with no route to the databases, queue, vector store, object storage or model proxy (`docker-compose.yml`), and/or (b) refuse a result whose final address (after redirects) is non-public, with a failing test in `tests/ingestion/test_web_crawler.py` using a fake client that reports a redirected final URL on a private address; document that the address check alone is not a boundary in `GRAPH_PATTERNS.md` pattern 50 *(FR-016)*

**Checkpoint**: US2 is *verified* only after T033–T036; its redirect boundary only after T037.

---

## Phase 5: User Story 3 — What the outside world returns is treated as data, cleaned and bounded (Priority: P1)

**Goal**: Scrubbed results, fail-closed remote capability, and framed, bounded web text.

**Independent Test**: A credential-shaped string from a fake remote tool is masked; a remote tool claiming read-only still needs approval; a hostile page is delimited wherever it is shown to the model.

### Tests for User Story 3

- [x] T038 [P] [US3] Capability default and override, remote metadata never trusted, per-tool schema, the async call dispatch, a remote error returned as text — in `tests/mcp/test_mcp_client.py` (`TestLoadRemoteTools`) *(FR-020, FR-021, FR-023, SC-004)*
- [x] T039 [P] [US3] No override is ever supplied for the sandbox bridge — in `tests/domains/test_sandbox_tools.py` (`test_never_supplies_capability_overrides_so_every_tool_defaults_to_outward`) *(FR-021, SC-004)*

### Implementation for User Story 3

- [x] T040 [US3] Reply scrubbing and the fail-closed capability map in `app/mcp/client.py`; scrubbing of every sandbox and page result through the shared timeout wrapper in `app/agent/tools.py::_arun_with_timeout` *(FR-020, FR-021, SC-003)*

### Open follow-ups for User Story 3 (not built) — **B25**

- [ ] T041 [US3] **B25 — write the failing tests first**: new `tests/domains/test_web_content_framing.py` asserting (a) the three reader tools return the page wrapped in the same untrusted-data delimiter the retrieval pre-fetch uses, (b) `package_lead_brief` wraps web-derived notes and caps each note and the total replay, (c) the delimiter cannot be closed early by page text. Fail today (reproduced — quickstart *Scenario B25*: no delimiter, 59,150 characters) *(FR-022, SC-008)*
- [ ] T042 [US3] **B25 — decide how to mark a web-derived note**: store it already wrapped (the cheapest — the framing travels with the row) or add a `source` column in a new numbered `postgres-init/16-lead-note-source.sql` (header says how an existing volume applies it) *(FR-022)*
- [ ] T043 [US3] **B25 — fix**: wrap the page text at the boundary in `app/ingestion/web_crawler.py` (or its three callers `app/domains/sales/tools.py`, `app/domains/support/tools.py`, `app/domains/ops/tools.py`), cap each note and the replay in `app/domains/sales/tools.py::_package_lead_brief_impl` and `app/domains/sales/store.py`, and add the delimiter's meaning to the notes' read path; update `GRAPH_PATTERNS.md` pattern 50 *(FR-022)*

**Checkpoint**: US3's scrubbing and fail-closed capability hold; its framing is verified only after T041–T043.

---

## Phase 6: User Story 4 — Tools from another program can be used, and this app's read-only data tools can be published (Priority: P2)

**Goal**: A client and two published servers, with the trust limits stated.

**Independent Test**: Load a fake catalogue and call a tool; start each server and call it with and without an identity.

### Tests for User Story 4

- [x] T044 [P] [US4] The directory server — lists its tool, refuses without a tenant or principal, a friendly invalid-department error, the enum value passed through, two tenants get two tenant parameters — in `tests/mcp/test_mcp_server.py` *(FR-024)*
- [x] T045 [P] [US4] The operations server — lists both tools, refuses without a principal, passes the status filter through — in `tests/mcp/test_ops_server.py` *(FR-024)*

### Implementation for User Story 4

- [x] T046 [P] [US4] `ecorp-structured-data` in `app/mcp/server.py` and `ecorp-ops` in `app/mcp/ops_server.py` (separate processes, explicit identity arguments, fixed parameterized queries) *(FR-024)*

### Open follow-ups for User Story 4 (not built) — **A4, A5**

- [ ] T047 [US4] **A5 — write the failing test first**: in `tests/domains/test_sandbox_tools.py` assert the bridge's API key is passed through the child's **environment** (`env={"OPEN_SANDBOX_API_KEY": …}`; the SDK's default child environment otherwise drops it) and **not** in the command-line arguments. Fails today (the existing test asserts it *is* in the arguments) *(FR-026)*
- [ ] T048 [US4] **A5 — fix**: pass the key via `env` in `app/domains/sandbox_tools.py` and update the existing test; allowlist the expected remote tool names in `app/mcp/client.py` (a tool the caller did not expect is dropped and counted) *(FR-026)*
- [ ] T049 [US4] **A4 — decide**: keep both servers local-only and say so in each server's `instructions` string in `app/mcp/server.py` and `app/mcp/ops_server.py`, and add a startup refusal when a non-stdio transport is requested; authentication is a prerequisite for any networked transport, recorded in `GRAPH_PATTERNS.md` pattern 21 *(FR-025)*

**Checkpoint**: US4 works as built for local use; T047–T049 harden it.

---

## Phase 7: User Story 5 — A dependency that is down degrades the feature, never the turn (Priority: P2)

**Goal**: Optional services, per-dependency breakers, counted transitions.

**Independent Test**: Start the application with both services stopped; it builds, answers, and the two tools report the outage within their timeouts.

### Tests for User Story 5

- [x] T050 [P] [US5] Retry, exhaustion, a non-retryable exception propagating and not counting, jittered capped delay, opening, a success never counting, half-open trial closing or re-opening, single-flight — in `tests/core/test_resilience.py` *(FR-018, FR-027)*

### Implementation for User Story 5

- [x] T051 [US5] Breaker use at both dependencies — `_OPENSANDBOX_BREAKER` in `app/domains/sandbox_tools.py`, `_CRAWL4AI_BREAKER` in `app/ingestion/web_crawler.py` *(FR-018, FR-027)*

### Open follow-ups for User Story 5 (not built) — **A7 (sandbox CI), A8**

- [ ] T052 [US5] **A7 — CI for the sandbox tier**: a job in `.github/workflows/ci.yml` that brings up the sandbox compose profile on a runner with a generated key and runs `pytest -m sandbox` (self-skips cleanly without the service, like the other real-backend tiers), so B23-style breakage fails a build *(FR-028, SC-010)*

**Checkpoint**: US5's degrade-don't-fail holds; its CI coverage arrives with T052.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T053 **A8 — configuration**: add `CRAWL4AI_SERVER_URL`, `OPENSANDBOX_MCP_DOMAIN`, `SANDBOX_IMAGE` and `SANDBOX_TTL_SECONDS` with a one-line why each to `.env.example` *(FR-029)*
- [ ] T054 **A8 — comments and docs**: fix the stale `app/mcp_server.py` / `app/mcp_client.py` paths in `requirements.txt`; correct "no network access" wording to what T026 verified; add B23–B27 and A1–A9 to "Extending Further" in `GRAPH_PATTERNS.md`; correct patterns 21, 28 and 50 *(FR-029)*
- [ ] T055 After each fix, re-run `quickstart.md`'s scenario for it and delete its row from `plan.md` *Complexity Tracking*

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup** → **Foundational** → **User Stories**. Everything `[x]` already exists.
- **US1, US2, US3** (P1) need only Phase 2; **US4** and **US5** (P2) are independent. **US1's T018–T020** share a Dockerfile edit with features 006 and 007 — land them as one change.
- **Polish** last.

### Open follow-ups — independence and PR boundaries

CLAUDE.md: one logical change per PR, ≤ ~400 hand-written lines.

| PR | Tasks | Touches | Notes |
|----|-------|---------|-------|
| 1 | T016–T017 (B24) | `sandbox_session.py`, the three wrappers, one new test file | **first** — tenant isolation; test-first |
| 2 | T033–T036 (B26, B27, A7 ranges) | `url_safety.py`, two callers, one new test file | test-first; small; independent |
| 3 | T018–T020, T021–T022 (B23, A9) | `Dockerfile`, `ci.yml`, `sandbox_tools.py`, `metrics.py`, `alerts.yml`, tests | one Dockerfile edit shared with features 006 and 007 |
| 4 | T041–T043 (B25) | `web_crawler.py`, three tools, the lead store, one new test file | needs the "mark a web note" decision |
| 5 | T023–T024 (A3) | the four tools, `sandbox_session.py`, tests | small; coordinate with feature 005's A7 |
| 6 | T037 (A1), T026 (A2) | `docker-compose.yml`, `opensandbox-server.toml`, tests | deployment decisions; `key` for A2 |
| 7 | T025 (A6), T047–T049 (A4, A5) | `sandbox_session.py`, `sandbox_tools.py`, `mcp/client.py`, servers | small, independent |
| 8 | T052 (A7 CI), T053–T055 (A8) | `ci.yml`, `.env.example`, docs | needs a runner with the compose service |

PRs 1, 2, 5 and 7 are mutually independent; run them in parallel. **Do PR 1 first.**

### Parallel opportunities

- Setup T001–T004 and Foundational T005–T008 are [P].
- After Phase 2, US1/US2/US3 in parallel; within a story every test task is [P].

## Parallel Example: User Story 2

```bash
# Tests together (different files):
Task: "T027 Crawler tests in tests/ingestion/test_web_crawler.py"
Task: "T028 Address-check tests in tests/ingestion/test_ingestor.py"
Task: "T029 Reader approval tests in tests/domains/{support,sales,ops}/test_domain.py"
# Implementation together:
Task: "T031 Render and breaker in app/ingestion/web_crawler.py"
Task: "T032 The three reader tools in app/domains/{sales,support,ops}/tools.py"
```

## Implementation Strategy

### As-built order (what happened)

The MCP client and the directory server are in the repository from 2026-08-28 (the package reorganization commit). The address check, the crawler and the sandbox integration arrived together on 09-07 — and, a day later, the sandbox was extended to the support and
sales products and both services became compose services, after a small local model reproducibly hallucinated an environment id and looped (which is why the sandbox is four flat tools). The breaker and retry layer followed on 09-27 once
the services were found to be briefly unreachable while starting. The Dockerfile (08-27) was not updated for any of it. The five defects sit at *seams the feature work never crossed*: a build file that predates the folders it needs, a key that was
made to fit a third party's rules, a rule written for retrieval that tools returning web text never inherited, and a standard-library notion of "private" that is narrower than "internal".

### Closing the open follow-ups (what to do next)

1. **PR 1 (B24)** now — the only defect against a non-negotiable principle, with a hash-based tag as the fix.
2. **PR 2 (B26, B27)** — small, independent, test-first.
3. **PR 3 (B23, A9)** — with features 006 and 007's image change.
4. **PR 4 (B25)** — framing and bounding web text.
5. **PRs 5–8** — bounds, deployment posture, MCP hardening, CI and docs.
6. Re-run quickstart, then delete each resolved row from plan.md *Complexity Tracking*.

### MVP scope

US1 + US2 + US3's first two layers (T001–T040) is the minimum that runs code, reads pages and keeps remote tools gated. Approval holds everywhere it is asserted, so the feature is safe to run *host-native with a single trusted
user* — but not **fully correct**: B24 lets distinct conversations meet in one sandbox, B25 lets a hostile page persist an instruction, B26 misses an internal range, B27 stalls a worker, and B23 removes the sandbox from containers.

## Notes

- `[x]` means "present", not "re-verified today" — only Tier 1 (131 passed) was re-run on 2026-10-03, plus the five reproductions, the image inspection, the address probe and the SDK's default-environment check.
- Tier 2, the live tier and the sandbox tier were **not** run by this batch; the sandbox service was not driven at all (its key is in a blocked file), so the real service's tag semantics and its egress behavior are **unverified**.
- Features 003 (the approval gate and the call-id protection), 005 (the product tool sets and the free-form argument bounds), 006 (the ingestion fetch), 007 (the image gap) and 008 (the breaker's metrics) own behavior this feature relies on.
- Do not run `make clean`, `clear-*` or `restart-all` while working these tasks; do not run commands in a sandbox you do not own to "test" B24 — use the stand-in service.
