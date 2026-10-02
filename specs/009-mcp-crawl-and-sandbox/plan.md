# Implementation Plan: MCP, Web Crawl and Sandbox Integrations

**Branch**: `009-mcp-crawl-and-sandbox` | **Date**: 2026-10-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/009-mcp-crawl-and-sandbox/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

Three doors out of the process, built the same way: a thin, domain-agnostic helper module; one flat, outward-declared tool per action in each product; a degrade-don't-fail posture behind a shared circuit breaker.

**Sandbox.** `app/domains/sandbox_tools.py::load_sandbox_tools` spawns `scripts/opensandbox_mcp_bridge.py` — a wrapper around the OpenSandbox MCP server that forces `use_server_proxy=True` so a host process can reach sandboxes on a
container bridge network — through the generic MCP client, with **no capability overrides** (so every remote tool is outward), a 10 s timeout, one retry on a connection-shaped failure and a breaker (3 failures, 30 s). It degrades to
`([], {})` rather than raising. `app/domains/sandbox_session.py` hides the ~19-tool catalogue behind four flat string-argument tools per product (`run_command_in_sandbox`, `run_python_in_sandbox`, `read_sandbox_file`,
`write_sandbox_file`): it finds a conversation's sandbox by a metadata tag on the service (`sandbox_list` filtered by the rewritten conversation id and state `RUNNING`), else creates one (image, tag, 1,800 s lifetime), and calls
`command_run`/`file_read`/`file_write` with `connect_if_missing=True` because every call spawns a fresh bridge process. A Python script is written to a fixed file and run as a second step so it never meets a shell. Results are rendered as
exit code, stdout and stderr. A successful catalogue load is cached; an empty one never is.

**Page reading.** `app/ingestion/web_crawler.py::render_url_to_markdown` runs the shared address check (`app/core/url_safety.py::assert_safe_url`: secure scheme, a host, every resolved address public), then asks a pooled `crawl4ai`
headless-browser container to render the page through its official docker client (bearer token set directly; no browser configuration sent), cut at 20,000 characters. A bare connection failure is retried (3 attempts) behind a breaker
(3 failures, 30 s); a request error or failed render is not. Three outward tools use it — `sales/tools.py::enrich_lead_from_website` (saves the page text as a note and returns a 500-character summary),
`support/tools.py::fetch_external_reference`, `ops/tools.py::check_vendor_status_page`.

**MCP.** `app/mcp/client.py::load_remote_tools` lists a stdio server's tools and wraps each with its own JSON schema; a call opens a fresh connection, scrubs the reply and returns a remote error as text; the capability comes only from the
caller's override map (default outward). `app/mcp/server.py` publishes `query_employees` and `app/mcp/ops_server.py` publishes `fetch_metrics_summary` and `list_recent_incidents`, each refusing a missing identity before touching data.
`app/core/resilience.py::CircuitBreaker` provides the retry and cooldown for the two services.

The plan records honestly that five defects — **B23** (no bridge script in the images, so no sandbox in a container), **B24** (conversation ids that collide after rewriting share a sandbox), **B25** (crawled text is returned,
stored and replayed unframed and unbounded), **B26** (the address check accepts 100.64.0.0/10) and **B27** (it blocks the event loop while resolving) — and nine smaller gaps sit around a design whose *approval, scrubbing and
fail-closed capability* properties hold where they are asserted. **B24 is a tenant-isolation defect** and the one to close first.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `mcp[cli]` 1.29.0 (pinned to the last 1.x because 2.0 removed the high-level server API), `crawl4ai` 0.9.3 (pinned exactly — it has broken its own API between releases; both the Python client and the
`unclecode/crawl4ai:0.9.3` server image), `opensandbox` 0.1.16 and `opensandbox-mcp` 0.1.1 (the sandbox SDK and its MCP server), `httpx`, `pydantic`, `langchain-core` (`StructuredTool`), `ipaddress`/`socket` from the standard library.
Services (compose): `crawl4ai` (default profile, port 11235, bearer token required), `opensandbox-server` (own `sandbox` profile; built from `docker/opensandbox-server.Dockerfile` and `.toml`; **bind-mounts the host's container-runtime socket
read-write**), the sandbox image `agent-core-demo-sandbox` (`docker/sandbox.Dockerfile`: Python 3.12 + numpy + pandas).

**Storage**: none new. The sandbox service keeps its own sqlite file in a named volume; lead notes are rows in the existing CRM table (feature 005); breaker state is process memory.

**Testing**: pytest hermetic tier — `tests/mcp/` (client: capability default and override, never trusting remote metadata, per-tool schema, error as text; both servers), `tests/domains/test_sandbox_session.py`
(lookup, create, fall-through, reuse, formatting, fence stripping, file tools, raw-call errors), `tests/domains/test_sandbox_tools.py` (bridge arguments, overrides never supplied, degradation, retry, breaker),
`tests/ingestion/test_web_crawler.py`, `tests/ingestion/test_ingestor.py::TestAssertSafeUrl`, `tests/core/test_resilience.py` (retry, breaker states, single-flight half-open), and each product's domain tests (every sandbox
tool declared outward; approval pause; crawl tools pause and run once approved). Real-service tiers — `tests/integration/test_web_crawler_live.py` (marker `integration`) and `tests/live/test_domain_crawl_tools_live.py` (marker `crawl`, run by `make test-live`);
`tests/live/test_opensandbox_mcp_live.py` and `test_sandbox_session_live.py` (marker `sandbox`, **manual**: `make test-sandbox`). **Not tested**: the images, anything that distinguishes two conversations, the address check beyond four cases,
framing of crawled text, sandbox output size, the redirect path.

**Target Platform**: host-native processes (all three doors work) and containers (**the sandbox cannot — B23**).

**Project Type**: Library modules + per-product tool wrappers + two MCP servers + a bridge script + three compose services.

**Performance Goals**: None asserted. Measured live once: with `use_server_proxy=True` a sandbox create returned in under 1.2 s versus 44+ s retrying an unreachable bridge address.

**Constraints**: sandbox call 60 s; catalogue listing 10 s with one retry after 1 s; sandbox lifetime 1,800 s (`SANDBOX_TTL_SECONDS`); page timeout 30 s and tool timeout 40 s; markdown cap 20,000 characters; breakers 3 failures and 30 s cooldown
for each service; page-read retries 3 attempts from 0.5 s.

**Scale/Scope**: 3 products × 4 sandbox tools, 3 page-reading tools, 3 MCP-published tools, 2 breakers, 3 compose services, 1 bridge script.

**Unknowns**: the real sandbox service's tag-filter semantics (the collision was reproduced against a stand-in) and whether its default sandbox has network egress (not verified: the API key lives in a blocked file); everything else is read from the
repository, the image or a reproduction.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | **Primary** | **FAIL on B24; A4 disclosed** | The sandbox holds whatever the assistant wrote into it — possibly one organization's data — and is found by a tag that is the conversation id *rewritten* to a restricted alphabet and truncated, with no organization, person or hash. Reproduced: `telegram:12345` and `telegram-12345` resolved to one environment and the second read the first's file. Feature 002's ownership check compares the raw id and cannot see it. The published servers take identity as caller-supplied arguments (disclosed; local standard-input use). Lead notes written by enrichment are tenant-scoped like all CRM rows. |
| II | Mandatory human approval (NN) | **Primary** | **PASS** | Every sandbox tool, the three page-readers and every remote tool default to `outward` and pause (`tests/domains/*/test_domain.py`: `test_every_sandbox_tool_present_is_declared_outward`, `…pauses_for_approval…`; `tests/mcp/test_mcp_client.py`: `…capability_defaults_to_outward…`, `…never_trusts_the_remote_tools_own_description_or_metadata`). The sandbox bridge's tools get **no** overrides. No scheduled job calls any of them. **Residual**: the approval shows the *address* or *command*, not what a page will say or what a stored note will later replay (B25). |
| III | Fixed, typed tools | Yes | **PASS with A3** | Flat string arguments, a non-blank validator, the injected call id. **A3**: none of the sandbox arguments is length-bounded (feature 005's A7 covers the whole class). |
| IV | Exactly-once side effects (NN) | Yes | **PASS** | Each outward tool goes through the call-id protection (feature 003). A sandbox command and a file write are **never** retried automatically — by design, with the reason stated in `resilience.py`; only the read-only catalogue listing and page reads are. Enrichment's note is a row keyed by the call id. The sandbox is disposable, so a duplicated command inside it is not a business side effect. |
| V | Bounded, observable failure | **Primary** | **PASS with B27 and B23's silence; one advisory** | Every wait is bounded (60 s, 10 s, 30/40 s), every retry names its exception class, breakers admit one half-open trial, the catalogue degrades and is never cached empty, and breaker and retry transitions are counted (feature 008). **B27**: the address check blocks the event loop. **B23, A9**: the degrade is *total and permanent in containers* and silent — a warning log and a tool message, no counter on "sandbox unavailable" and no alert. |
| VI | Untrusted content is data | **Primary** | **FAIL on B25, B26, A1; PASS on scrubbing** | Scrubbing covers all three paths (the sandbox tools through the shared timeout wrapper, the MCP client explicitly) and a remote tool's capability is fail-closed. But crawled pages — the most hostile text in the system — are returned, stored and replayed with **no `<retrieved_document>` framing**, and replayed without bound, although the system prompt promises tool-returned framed content is treated as data. The address check misses a range (B26) and covers one address once (A1). |
| VII | Test discipline | Yes | **PASS with the known gap (A7)** | 131 hermetic tests pass. The sandbox tier is manual, the address check has four cases, nothing distinguishes two conversations' sandboxes, and nothing inspects the image — how B23–B26 shipped. |
| VIII | Why-first docs, honest gaps | Yes | **PASS with drift (A8)** | The code is unusually candid: it discloses the rebinding gap, the unauthenticated published servers, the missing tenant scoping on sandboxes ("a shared operational resource, not tenant data" — which B24 shows is no longer true once the assistant writes data into it) and the third-party read bug. Drift: four settings missing from the example environment, stale file paths in dependency comments, an unverified "no egress" claim. |
| — | *Composition* constraint | Yes | **PASS** | The sandbox tools are in each product's fixed tool set and its approval allowlist; no product forks the graph. |
| — | *Configuration* constraint | Yes | **FAIL on A8** | Four of six tunables have no `.env.example` entry. |

**Gate result (pre-research)**: **B24 is a defect against a NON-NEGOTIABLE principle (I)** — a boundary around possibly tenant-owned data keyed by a rewritable string — and should be closed before any other task in this
feature. B25 and B26 are Principle VI defects (framing; an incomplete address check); B27 is a Principle V defect; B23 makes a whole feature absent in containers. All five were *reproduced* (B23 in the real image; B24 against a stand-in
sandbox service with the real tool shapes; B25 with a patched crawler; B26 with literal addresses; B27 with a stand-in resolution). They are *defects*, not justified exceptions; the plan proceeds because it describes shipped code.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged. Writing the "what a conversation's sandbox is keyed by" table in `data-model.md` §1 made B24 visible as a *lossy mapping*
(the rewrite is not injective), and writing the "where untrusted text re-enters the model" table in §4 made B25 visible as a *round trip* — web text goes out through an approved call and comes back through an unapproved read.

## Project Structure

### Documentation (this feature)

```text
specs/009-mcp-crawl-and-sandbox/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the incidents behind each; B23–B27 and A1–A8 as findings
├── data-model.md                  # Phase 1 — the sandbox key, the page read, the lead note, the remote tool, the breaker
├── quickstart.md                  # Phase 1 — runnable checks per tier, incl. the B23–B27 reproductions
├── contracts/
│   ├── sandbox-tools.md           # the four flat tools, the session lifecycle, results, failures
│   ├── page-reading.md            # the address check, the render, the three tools, the limits
│   └── mcp.md                     # the client (capability fail-closed), the two published servers
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── domains/
│   ├── sandbox_session.py         # the four tools' shared logic: tag, find-or-create, run, read, write, format
│   ├── sandbox_tools.py           # load_sandbox_tools: spawn the bridge, no overrides, retry once, breaker, degrade
│   └── {support,ops,sales}/tools.py   # the per-product wrappers; the three page-reading tools; _SANDBOX_TOOLS
├── ingestion/web_crawler.py       # render_url_to_markdown, _crawl (retry + breaker), CrawlFailed, the 20,000-character cap
├── core/
│   ├── url_safety.py              # assert_safe_url (the shared address check), UnsafeURLError
│   └── resilience.py              # CircuitBreaker, CircuitOpenError, retry with jittered back-off
└── mcp/
    ├── client.py                  # load_remote_tools, _call_remote_tool, _wrap_remote_tool
    ├── server.py                  # query_employees over stdio
    └── ops_server.py              # fetch_metrics_summary, list_recent_incidents over stdio
scripts/opensandbox_mcp_bridge.py  # the bridge (use_server_proxy=True) — absent from the images (B23)
docker/opensandbox-server.Dockerfile · docker/opensandbox-server.toml · docker/sandbox.Dockerfile
docker-compose.yml                 # crawl4ai, opensandbox-server (sandbox profile); docker-compose.prod.yml: env only
Makefile                           # sandbox-up, sandbox-build, test-sandbox, mcp-serve, mcp-serve-ops
tests/mcp/ · tests/domains/test_sandbox_*.py · tests/ingestion/test_web_crawler.py · tests/core/test_resilience.py · tests/live/, tests/integration/
```

**Structure Decision**: One helper per door, one wrapper per product, so a fix reaches every product at once. The two external services share one breaker implementation but not one breaker (a sandbox outage must not stop page reads).
The sandbox logic is deliberately *code in the app* rather than the model driving the service's raw catalogue, because a small local model reproducibly hallucinated an environment id and looped (pattern 50).

## Complexity Tracking

> Filled because the Constitution Check found five defects and several gaps. Defects are listed without a justification column: they are simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B24 (defect, open — fix first)** — a sandbox is found by the rewritten conversation id alone. Reproduced: distinct ids, one environment, a file read across them. | Not needed — the rewrite exists only to satisfy the sandbox service's tag rules and was never made injective or tenant-aware ("collisions are theoretical at this app's scale"). | Tag with two keys — a hash of the tenant and a hash of the raw conversation id (≤ 63 characters each, restricted alphabet) — and match **both** on lookup; verify the found environment's tags before reuse; failing test with the colliding pairs first. |
| **B23 (defect, open)** — no `scripts/` in the images, so the bridge cannot start. Reproduced in the real image: 0 tools. | Not needed — the Dockerfile predates the bridge; the compose services arrived without updating it. | Copy the bridge script (and decide whether all of `scripts/` ships — features 006, 007) and add an image smoke that the bridge starts; see feature 007's T053. A mounted volume would work but leaves the production image broken by default. |
| **B25 (defect, open)** — crawled text is returned, stored and replayed unframed; the replay is unbounded. Reproduced. | Not needed — the framing rule was written for retrieval and the page tools return plain strings. | Wrap the page text in the same delimiter at the tool boundary and when `package_lead_brief` replays web-derived notes (mark them at write time); cap each note and the replay. A decision on how to tell a web note from a person's. |
| **B26 (defect, open)** — the shared carrier-grade range passes the address check. Reproduced. | Not needed — the standard library's "private" does not include it. | Accept only addresses where `is_global` is true, and add range tests (IPv6, link-local, mapped, shared space, NAT64). |
| **B27 (defect, open)** — the address check blocks the event loop on name resolution. Reproduced. | Not needed — a synchronous helper called from async code. | `await asyncio.to_thread(assert_safe_url, url)` at both callers, or an async resolver; a heartbeat test. |
| **A1** — one address, validated once; redirects, navigation, subresources and the browser's own resolution are unguarded; the browser shares the default compose network. | A URL check is the cheap defence; the full one needs network policy. | Put the browser container on a network with no route to internal services (egress policy), and refuse a result whose final address is non-public; document that the address check alone is not a boundary. |
| **A2** — sandbox posture: bridge networking, no memory or CPU limits, no per-tenant cap, a read-write runtime socket, an unverified "no egress" claim. | Defaults adapted from the service's own example. | Set limits and a network policy in the server configuration, cap sandboxes per tenant, verify and test the egress claim, state the socket's risk in the README. |
| **A3** — sandbox arguments and results unbounded. | Bounds arrived for the page reader but not the sandbox. | `max_length` constants on the four tools' arguments and a result cap with a truncation marker (reuse the page reader's 20,000). |
| **A4** — published servers trust caller-supplied identity. | A demo of the query-layer scoping, disclosed in the module docstring. | Keep local-only and say so in the server's own instructions; add authentication before any networked transport. |
| **A5** — remote text passes through; the bridge key is on a command line. | The only caller is the local bridge. | Pass the key through the child's environment (the bridge already falls back to it); allowlist the expected remote tool names. |
| **A6** — `read_sandbox_file` is offered though it fails against the pinned release. | A third-party bug with a documented workaround. | Implement it as `cat` internally, or drop the tool until the pin moves. |
| **A7** — the sandbox path is not in CI; the address check has four test cases. | The sandbox tier needs a service and a key. | Range tests (hermetic); an image smoke for the bridge; a CI job for the sandbox tier on a runner with the compose service. |
| **A8** — four settings missing from the example environment; stale comments. | Written feature by feature. | Add the entries, fix the paths, add the egress test. |
| **A9** — a broken bridge is not retried, not counted by the breaker, not counted by any metric. | The breaker was written to ride out *transient* connection failures and deliberately ignores other kinds. | One counter on the catalogue-load degrade (`agent_sandbox_unavailable_total{reason}`), and a rule when it is non-zero for 15 minutes; keep the no-retry rule. |
