# Data Model: MCP, Web Crawl and Sandbox Integrations

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

No tables are added. State is: a sandbox on an external service (keyed by a rewritten string), a page read (transient), lead-note rows (existing CRM table), and per-process breaker state.

---

## 1. What a conversation's sandbox is keyed by (B24)

`_sanitize_thread_id_for_metadata(thread_id)`: replace every character outside `[A-Za-z0-9_.-]` with `-`, cut to 63 characters, strip leading and trailing `_`, `-` and `.`; an empty result becomes `"thread"`.
The result is the **only** value in the sandbox's tag `{"agent_core_thread": <value>}` and the **only** lookup key (`sandbox_list` filter on that tag and state `RUNNING`, first hit wins).

| Raw conversation id | Tag value | Note |
|---------------------|-----------|------|
| `f3c2…` (a random UUID, the request default) | the same, 36 characters | unguessable; no collision in practice |
| `telegram:12345` (the chat channel's id) | `telegram-12345` | the colon is rewritten |
| `telegram-12345` (any web client may send this) | `telegram-12345` | **the same tag** — one environment for two ids |
| `demo:1` and `demo-1` and `demo 1` | `demo-1` | three ids, one tag |
| `x…x`+`A` and `x…x`+`B` (63 `x` first) | `x…x` (63) | truncation merges them |
| `…` (only punctuation) | `thread` | every such id shares `thread` |

**Not in the key**: tenant, principal, domain. **What guards a raw id**: feature 002's conversation-ownership check (compares the raw id) and the reserved-prefix guard (matches `telegram:` only) — neither sees the rewritten value.
**Intended key** (task T001): a collision-free hash of the tenant and of the raw id, matched on **both**, plus a check of the found environment's tags before reuse.

## 2. The sandbox, as the service reports it

| Field | Value |
|-------|-------|
| id | assigned by the service; held only inside one call — never shown to the model |
| metadata | `{"agent_core_thread": <tag>}` |
| image | `SANDBOX_IMAGE` (default `agent-core-demo-sandbox:latest`: Python 3.12 + numpy + pandas) |
| lifetime | `SANDBOX_TTL_SECONDS` = 1,800 s from creation (not renewed — renewal is disabled); the server's ceiling is 86,400 s |
| contents | files the assistant wrote and whatever a command left; **lost silently** at expiry — the next call creates an empty sandbox |
| network / limits | bridge networking, host-published port allocation, `pids_limit = 4096`; no memory or CPU limit configured; "no egress by default" is a claim about the third-party default and is **unverified** (A2) |

### Calls made to the service (flat, hidden from the model)

| Operation | Service tool | Notes |
|-----------|--------------|-------|
| find | `sandbox_list(filter={"metadata": {...}, "states": ["RUNNING"]})` | a failure returns "none found" and falls through |
| create | `sandbox_create(image, metadata, timeout_seconds)` | a failure, or a reply with no `sandbox_id`, raises `SandboxCallFailed` |
| run | `command_run(sandbox_id, command, connect_if_missing=True)` | never retried |
| write | `file_write(sandbox_id, path, content, connect_if_missing=True)` | never retried |
| read | `file_read(sandbox_id, path, connect_if_missing=True)` | fails against the pinned release right after a write (A6) |

Result rendering: `exit code: <n>`, then `stdout:\n<text>` (or `stdout: (empty)`), then `stderr:\n<text>` only if non-empty. **No size cap** (A3).

## 3. Tool inventory

| Tool | Products | Capability | Timeout | Arguments | Backed by |
|------|----------|-----------|---------|-----------|-----------|
| `run_command_in_sandbox` | support, ops, sales | `outward` | 60 s | `command` (non-blank, **unbounded**) | `sandbox_session.run_command_in_sandbox_impl` |
| `run_python_in_sandbox` | support, ops, sales | `outward` | 60 s | `script` (non-blank, **unbounded**) | `…run_python_in_sandbox_impl` |
| `read_sandbox_file` | support, ops, sales | `outward` | 60 s | `path` (**unbounded**) | `…read_sandbox_file_impl` |
| `write_sandbox_file` | support, ops, sales | `outward` | 60 s | `path`, `content` (**unbounded**) | `…write_sandbox_file_impl` |
| `enrich_lead_from_website` | sales | `outward` | 40 s | `contact`, `url` | `render_url_to_markdown` + `store.append_lead_note` |
| `fetch_external_reference` | support | `outward` | 40 s | `url` | `render_url_to_markdown` |
| `check_vendor_status_page` | ops | `outward` | 40 s | `url` | `render_url_to_markdown` |
| `query_employees` (MCP) | — (a published server) | n/a | — | `tenant`, `principal`, `department?`, `name_contains?` | `sql_store` (feature 002/005) |
| `fetch_metrics_summary`, `list_recent_incidents` (MCP) | — (a published server) | n/a | — | `principal`, `status?` | the ops store and the monitoring query |

Every row except the published-server rows goes through the call-id protection (feature 003) and pauses for approval. Remote tools loaded from another program: capability `= overrides.get(name, "outward")`; the sandbox bridge passes **no** overrides.

## 4. Where untrusted text re-enters the model (B25)

Web text is attacker-controlled by definition. It reaches the model through **three** doors, only the first of which has an approval:

| Door | Content | Approval | Framed as data? | Bounded? |
|------|---------|----------|-----------------|----------|
| A page-reading tool's result | up to 20,000 characters of the page | **yes** — but it shows the *address*, not the page | **no** | yes (cap + marker) |
| `enrich_lead_from_website` stores the page | the same text as a lead note row, prefixed `Website research (<url>):` | yes | n/a (stored) | the 20,000 cap, **per call** |
| `package_lead_brief` replays *every* note | all notes aggregated, verbatim | **no** — read-only | **no** | **no** (59,150 characters after three enrichments) |

The lead brief is also available to the sales lead-research specialist (feature 007). A stored instruction is therefore re-injected on every brief, without an approval, for as long as the note exists.

## 5. A page read

| Parameter | Value |
|-----------|-------|
| scheme | `https` only |
| address check | `assert_safe_url` — runs on the event loop, synchronously (B27) |
| renderer | a pooled headless-browser container, one fresh client per attempt, bearer token from `CRAWL4AI_API_TOKEN`, no browser configuration sent |
| cache | bypassed |
| page timeout | 30 s (`CRAWL_TIMEOUT_SECONDS`); tool timeout 40 s |
| retries | 3 attempts from 0.5 s, **connection failures only**; a request error or `success=False` is final |
| breaker | 3 consecutive exhausted failures → open for 30 s → one half-open trial |
| output | markdown, cut at 20,000 characters with `[truncated: page content exceeds the fetch limit]` |
| failure text | one line: `could not render <url>: <first line of the server's message>` / `could not reach the crawl4ai server for <url>: …` |
| redirects, subresources | **not checked** (A1) |

## 6. The address check — decision table

Refused if the scheme is not `https`, there is no host, resolution fails, or **any** resolved address is loopback, private, link-local, reserved, multicast or unspecified. Probed 2026-10-03 with IP-literal hosts (no network):

| Probe | Result | | Probe | Result |
|-------|--------|-|-------|--------|
| `127.0.0.1` | refused | | `[64:ff9b::7f00:1]` (NAT64 → loopback) | refused |
| `[::1]` | refused | | `[64:ff9b::a9fe:a9fe]` (NAT64 → metadata) | refused |
| `[::ffff:127.0.0.1]` | refused | | `[2002:7f00:1::]` (6to4 → loopback) | refused |
| `[::ffff:10.0.0.1]` | refused | | `2130706433` (integer loopback) | refused |
| `169.254.169.254` | refused | | `0x7f.1` | refused |
| `192.0.0.192` | refused | | `[fd00:ec2::254]` | refused |
| `[fe80::1]` | refused | | `8.8.8.8` | allowed (correct) |
| **`100.100.100.200`** | **allowed (B26)** | | **`100.64.0.1`** | **allowed (B26)** |

## 7. Retry and breaker parameters

| Dependency | Breaker name | Failures to open | Cooldown | Retry on | Attempts | Base delay | Per-attempt timeout |
|------------|--------------|------------------|----------|----------|----------|------------|--------------------|
| sandbox catalogue listing | `opensandbox_mcp` | 3 | 30 s | `ConnectionError`, `TimeoutError` | 2 | 1.0 s | 10 s |
| page render | `crawl4ai` | 3 | 30 s | the browser client's `ConnectionError` | 3 | 0.5 s | 40 s |
| sandbox commands, file reads and writes | *(none)* | — | — | *(never retried)* | 1 | — | 60 s |

Breaker state is per process, in memory; transitions increment `agent_circuit_breaker_{opened,rejected,half_open}_total{dependency}` and retries `agent_tool_retry_total{dependency}` (feature 008: on no dashboard or alert).

## 8. The MCP surfaces

| Surface | Direction | Transport | Identity | Notes |
|---------|-----------|-----------|----------|-------|
| `app/mcp/client.py` | this app consumes a remote catalogue | stdio, a child process **per call** | n/a | name, description and JSON schema taken from the remote as given; reply scrubbed; remote error → `Remote tool error: …` text; capability from the local caller only |
| `ecorp-structured-data` (`app/mcp/server.py`) | this app publishes `query_employees` | stdio | `tenant`, `principal` as **caller-supplied arguments** (A4) | missing either → `Refused`; fixed parameterized query; department a closed enum |
| `ecorp-ops` (`app/mcp/ops_server.py`) | this app publishes two ops tools | stdio | `principal` argument; tenant fixed to the default | read-only; no free-form query |

## 9. Settings

| Name | Value | `.env.example`? |
|------|-------|-----------------|
| `CRAWL4AI_SERVER_URL` | `http://localhost:11235` (compose: `http://crawl4ai:11235`) | **no** (A8) |
| `CRAWL4AI_API_TOKEN` | empty; required by the server | yes |
| `OPENSANDBOX_MCP_DOMAIN` | `localhost:8090` (compose: `opensandbox-server:8090`) | **no** |
| `OPENSANDBOX_API_KEY` | empty; passed to the bridge on a **command line** (A5) | yes |
| `SANDBOX_IMAGE` | `agent-core-demo-sandbox:latest` | **no** |
| `SANDBOX_TTL_SECONDS` | `1800` | **no** |
| `CRAWL_TIMEOUT_SECONDS` / `CRAWL_TOOL_TIMEOUT_SECONDS` / `_MAX_MARKDOWN_CHARS` | 30 / 40 / 20,000 | constants, `web_crawler.py` |
| `SANDBOX_CALL_TIMEOUT_SECONDS` | 60 | constant, `sandbox_session.py` |
