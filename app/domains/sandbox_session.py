"""Pure helper logic behind each domain's own three sandbox tools —
run_command_in_sandbox, read_sandbox_file, write_sandbox_file, wrapped
separately per domain (app/domains/{ops,support,sales}/tools.py — each does
its own ctx checks and writes its own domain-specific docstring, the same
"one shared impl, one @tool wrapper per domain" shape
render_url_to_markdown already has for the crawl4ai tools) — built on top
of OpenSandbox's raw MCP catalog (app/domains/sandbox_tools.py,
GRAPH_PATTERNS.md pattern 50) instead of exposing that catalog to the LLM
directly, the way app/domains/ops/domain.py originally did.

Domain-agnostic on purpose, not an accident this file only lives under
app/domains/ (not app/domains/ops/ anymore) — verified directly nothing
below ever referenced "ops" in its actual logic, only in stale prose; the
relocation just made the file's location match what was already true.

## Why this exists: a real, live-verified model-capability finding

Handing a small local model (qwen2.5:3b) OpenSandbox's raw ~19-tool
catalog directly — a stateful create → connect → run lifecycle, deeply
nested optional schemas — produced real, reproducible failures verified
live against the real model and the real MCP bridge: it hallucinated a
sandbox_id instead of creating one, then (after a validation error)
reached for sandbox_connect with no arguments instead of sandbox_create,
repeatedly, until the graph's own no-progress safety net (pattern 34) cut
the loop short. A prompt-only fix ("always create first") did NOT change
the model's very first action in a second live run — a real ceiling on
this model's instruction-following for a multi-step external API, not
something more prompt text was going to fix.

Every OTHER tool in this app is 1-3 flat fields (calculator, create_ticket,
check_vendor_status_page) — that's deliberate, and it's exactly what small
models handle reliably. This module gives the ops agent the SAME shape for
sandbox access: one flat string argument per tool, the entire create/
reuse/connect lifecycle handled here in code, never exposed to the model
at all. The model never sees a sandbox_id, never calls sandbox_create or
sandbox_connect.

## Per-thread reuse, done server-side, not via a local cache

Each thread's investigation gets ONE sandbox, tagged with metadata
`{SANDBOX_METADATA_KEY: thread_id}` at creation time, found again on later
calls via sandbox_list's own metadata filter — verified directly (reading
opensandbox_mcp's own source, see below) that `SandboxFilter` supports
this. This is server-side lookup, not a process-local dict: it stays
correct across this app's horizontally-scaled agent-worker processes
(GRAPH_PATTERNS.md pattern 43) — a resume picked up by a DIFFERENT worker
than the one that created the sandbox still finds it, which a local cache
never would.

`connect_if_missing=True` is passed on every command_run/file_read/
file_write call, unconditionally — verified live and confirmed necessary:
`load_remote_tools`'s "one connection per call" design (app/mcp/client.py)
means each wrapped tool call spawns a FRESH opensandbox-mcp subprocess, so
the bridge's own "local registry" of known sandboxes (what its
`sandbox_connect`-or-error message refers to) never persists between
separate tool invocations anyway — every call after the one that created
the sandbox would otherwise hit "not found in local registry" regardless
of how correct the sandbox_id is.

## Response shapes: read from OpenSandbox's own source, then confirmed live

The shapes below were originally read directly from the installed
`opensandbox_mcp`/`opensandbox` packages' own Pydantic models (ground
truth, not inference) because this session's opensandbox-server hit a real
HTTP 405 on every `sandbox_create` attempt at the time — since root-caused
to this app's own `opensandbox_mcp_domain` port colliding with
`docker-compose.yml`'s `open-webui` (app/core/config.py's own comment,
GRAPH_PATTERNS.md pattern 50), not an OpenSandbox defect. With that fixed
(port 8090 + real `--api-key` auth, `make sandbox-up`), a full live round
trip now confirms these shapes against a REAL successful response, not
just source:
- sandbox_create -> `{"sandbox_id": ..., "info": {...}}`
- sandbox_list -> `{"sandbox_infos": [{"id": ..., "status": {"state": "RUNNING", ...}, "metadata": {...}, ...}], "pagination": {...}}`
- command_run -> Execution: `{"exit_code": int|None, "logs": {"stdout": [{"text": ...}], "stderr": [{"text": ...}]}, ...}`
- file_read -> `{"path": ..., "content": ...}`
- file_write -> `{"status": "written"}`
Hermetically tested against these exact shapes in
tests/domains/test_sandbox_session.py.
"""
import json
import logging
import re

from langchain_core.tools import BaseTool

from app.core.config import SANDBOX_IMAGE, SANDBOX_TTL_SECONDS
from app.domains.sandbox_tools import load_sandbox_tools

logger = logging.getLogger(__name__)

SANDBOX_METADATA_KEY = "agent_core_thread"
SANDBOX_CALL_TIMEOUT_SECONDS = 60  # a real command_run (e.g. a slow
# script) can legitimately take longer than a plain HTTP round trip —
# its own named budget, same "this takes longer than the default"
# reasoning every other override of app/agent/tools.py's
# TOOL_TIMEOUT_SECONDS already uses in this app.
#
# Two real production timeouts (Langfuse traces d9034aaa.../30f20dfc...,
# 2026-09-08) hit this budget while `get_or_create_sandbox_id` was still
# in progress, both on trivial commands — briefly bumped to 150s as a
# band-aid, then REVERTED once the actual bug was found and fixed (don't
# read this constant's own git history as "150 was tried and abandoned for
# no reason" — it was a workaround for a real bug, removed once that bug
# was gone). Root cause, found by tracing the real request URLs in
# debug-level httpx logs: the OpenSandbox SDK's `ConnectionConfig.
# use_server_proxy` defaults to `False`, and `opensandbox-mcp==0.1.1`'s own
# CLI has no flag/env var to override it — so `opensandbox-mcp` (a bare
# host process, app/domains/sandbox_tools.py) was trying to reach each
# sandbox directly at its Docker bridge-network IP (e.g.
# `172.19.0.13:port`), an address genuinely UNREACHABLE from the host on
# Docker Desktop for Mac, not merely slow. A raw `Sandbox.create()` call
# confirmed this directly: 44+ seconds stuck retrying against that address
# with `use_server_proxy=False`, under 1.2s with it `True`. Fixed via
# `scripts/opensandbox_mcp_bridge.py` (see its own docstring) — normal
# calls now complete in low single-digit seconds, so 60s is generous
# headroom again, not a tight fit.


class SandboxCallFailed(Exception):
    """A raw OpenSandbox MCP tool call returned an error, or a success
    response this module couldn't parse into the shape it expects — an
    expected, caller-facing outcome (propagates to the calling tool's
    normal exception handling, app/agent/graph.py's handle_tool_errors,
    same as every other tool's uncaught exception), not a bug to let
    surface as a raw traceback."""


_raw_sandbox_tools_cache: dict[str, BaseTool] = {}


def load_raw_sandbox_tools() -> dict[str, BaseTool]:
    """OpenSandbox's raw MCP tool catalog as a {name: tool} lookup, empty
    if opensandbox-mcp isn't installed/reachable (see
    app/domains/sandbox_tools.py's own docstring for why that degrade is
    safe to rely on). Called by each of app/domains/{ops,support,sales}/
    tools.py's own per-call impl wrappers — NOT at their module import
    time (see those files' own comments for why that distinction matters).

    Self-healing, not cache-once-forever: a successful (non-empty) result
    is cached for the rest of the process's life (repeating a working
    catalog listing on every call would be pure waste, and now that THREE
    domains call this, a process that imports all three — app/domains/
    registry.py, most test runs — would otherwise pay that cost three
    times over). An EMPTY result is never cached, so every call made while
    opensandbox-mcp is unreachable retries the real connection attempt,
    bounded by `_SANDBOX_LIST_TIMEOUT_SECONDS` (app/domains/sandbox_tools.py)
    each time.

    Found live, not hypothetical: this used to cache a `None` sentinel
    forever after the first call, computed once at each domain module's
    import time. A dev server that finished booting before
    `opensandbox-server` finished starting cached an empty result
    permanently — every sandbox tool then stayed invisible to every domain
    for that process's entire remaining lifetime, even hours after the
    container became healthy, with no restart to fix it short of actually
    restarting the process. The model, unable to find run_command_in_sandbox,
    hallucinated a nonexistent run_subagent name trying to route around the
    gap instead of ever getting a clear error to act on or surface to a
    human (Langfuse trace 806125c9, 2026-09-08). Every caller gets the
    exact same dict (mutating it would affect every domain, but nothing
    here ever does)."""
    global _raw_sandbox_tools_cache
    if not _raw_sandbox_tools_cache:
        raw_tools, _capabilities = load_sandbox_tools()
        _raw_sandbox_tools_cache = {t.name: t for t in raw_tools}
    return _raw_sandbox_tools_cache


def _call_raw_tool(raw: dict[str, BaseTool], name: str, **kwargs) -> dict:
    """Calls one of OpenSandbox's raw MCP tools by name and parses its
    result into a dict. `.invoke(...)` (the standard Runnable interface
    every BaseTool implements), not the StructuredTool-specific `.func` —
    load_sandbox_tools() only promises BaseTool, and this goes through the
    tool's normal invocation path (args_schema validation included)
    rather than assuming a particular subclass's escape hatch. Raises
    SandboxCallFailed on a remote tool error (app/mcp/client.py's own
    `"Remote tool error: ..."` prefix) or on a success response that isn't
    the JSON object this module expects — never silently returns a
    partial/wrong shape."""
    raw_text = raw[name].invoke(kwargs)
    if raw_text.startswith("Remote tool error:"):
        raise SandboxCallFailed(raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SandboxCallFailed(f"{name} returned an unparseable response: {raw_text[:300]}") from exc
    if not isinstance(parsed, dict):
        raise SandboxCallFailed(f"{name} returned an unexpected shape: {raw_text[:300]}")
    return parsed


_INVALID_METADATA_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_thread_id_for_metadata(thread_id: str) -> str:
    """OpenSandbox's own sandbox-metadata VALUE rules (confirmed live, not
    guessed, from a real `sandbox_create` rejection): 63 chars or less,
    start/end alphanumeric, only alphanumeric/-/_/. in between. Real
    thread_ids don't necessarily satisfy this — app/channels/telegram.py's
    own `_thread_id_for_chat` returns `f"telegram:{chat_id}"`, and that
    colon alone made every sandbox_create/sandbox_list call for a Telegram
    thread fail with SANDBOX::INVALID_METADATA_LABEL, unconditionally, for
    every domain's sandbox tools. Sanitized HERE, not by changing
    thread_id's own format at the source — thread_id is also the Postgres
    checkpointer's key and the Langfuse trace's thread_id metadata, and
    this is OpenSandbox's own constraint alone, not a property thread_id
    needs to satisfy generally. Collisions between two thread_ids that
    happen to sanitize to the same string are a theoretical, not a
    practical, concern at this app's scale."""
    sanitized = _INVALID_METADATA_CHARS.sub("-", thread_id)[:63].strip("_-.")
    return sanitized or "thread"


def _find_existing_sandbox_id(raw: dict[str, BaseTool], sandbox_metadata_value: str) -> str | None:
    """Looks up a RUNNING sandbox already tagged for this thread — see
    module docstring for why this is a server-side sandbox_list metadata
    filter, not a local cache. Returns None (not found, or the lookup
    itself failed) rather than raising: a failed lookup should fall
    through to creating a fresh sandbox, not abort the whole call."""
    try:
        result = _call_raw_tool(
            raw,
            "sandbox_list",
            filter={"metadata": {SANDBOX_METADATA_KEY: sandbox_metadata_value}, "states": ["RUNNING"]},
        )
    except SandboxCallFailed as exc:
        logger.warning("sandbox_list_failed", extra={"error": str(exc)[:300]})
        return None
    infos = result.get("sandbox_infos") or []
    if not infos:
        return None
    return infos[0].get("id")


def get_or_create_sandbox_id(raw: dict[str, BaseTool], thread_id: str) -> str:
    """The one lifecycle decision every tool in this module makes before
    anything else: reuse this thread's existing sandbox if sandbox_list
    finds one, otherwise create a fresh one tagged for this thread. Raises
    SandboxCallFailed if creation itself fails (e.g. opensandbox-server
    unreachable or misconfigured — see GRAPH_PATTERNS.md pattern 50) —
    there's nothing to fall back to at that point."""
    sandbox_metadata_value = _sanitize_thread_id_for_metadata(thread_id)
    existing = _find_existing_sandbox_id(raw, sandbox_metadata_value)
    if existing:
        return existing
    created = _call_raw_tool(
        raw,
        "sandbox_create",
        image=SANDBOX_IMAGE,
        metadata={SANDBOX_METADATA_KEY: sandbox_metadata_value},
        timeout_seconds=SANDBOX_TTL_SECONDS,
    )
    sandbox_id = created.get("sandbox_id")
    if not sandbox_id:
        raise SandboxCallFailed(f"sandbox_create did not return a sandbox_id: {created}")
    return sandbox_id


def _format_execution(execution: dict) -> str:
    """Execution -> a short, plain-text summary (exit code + stdout +
    stderr, stderr only when non-empty) — see module docstring for exactly
    where this shape comes from. Deliberately NOT the raw JSON: the whole
    point of this module is giving the model something as simple to read
    as every other tool's result, not a nested object it has to navigate
    itself."""
    exit_code = execution.get("exit_code")
    logs = execution.get("logs") or {}
    stdout = "".join(m.get("text", "") for m in (logs.get("stdout") or []))
    stderr = "".join(m.get("text", "") for m in (logs.get("stderr") or []))
    lines = [f"exit code: {exit_code}"]
    lines.append(f"stdout:\n{stdout}" if stdout else "stdout: (empty)")
    if stderr:
        lines.append(f"stderr:\n{stderr}")
    return "\n".join(lines)


def run_command_in_sandbox_impl(command: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    sandbox_id = get_or_create_sandbox_id(raw, thread_id)
    execution = _call_raw_tool(raw, "command_run", sandbox_id=sandbox_id, command=command, connect_if_missing=True)
    return _format_execution(execution)


# Fixed, framework-managed path — every run_python_in_sandbox call
# overwrites it and runs it fresh, so there's no need for a unique name
# per call (see that function's own docstring for why this exists at
# all). The leading underscore is a plain naming convention, not an
# OpenSandbox/OS-level privacy mechanism — it just signals "this file is
# this tool's own scratch space," distinct from any path the model
# deliberately writes itself via write_sandbox_file.
_RUN_PYTHON_SCRIPT_PATH = "_run_python_in_sandbox.py"


def _strip_markdown_fence(script: str) -> str:
    """If the model wrapped its script in a markdown code fence
    (```python\\n...\\n```), strip it rather than let a `SyntaxError` on
    line 1 waste the call — a real, live-verified mistake, found
    immediately after this tool shipped: a fenced code block is how the
    model normally SHOWS code to a human in its own prose, so it reached
    for the identical shape when passing code as a tool parameter,
    without registering that `script` needs raw source, not markdown.

    Only activates when the script STARTS with ``` — three literal
    backticks can never legally begin a Python statement, so this is an
    unambiguous "this is markdown, not source" signal; a script that
    doesn't start this way is returned completely untouched, so a stray
    ``` that's legitimately part of the script's own content (inside a
    string, say) is never at risk. Handles both fence shapes seen live:
    a clean closing ``` on its own line, AND one glued directly onto the
    end of the last code line with no newline before it (the model
    produced both in different calls of the same investigation)."""
    stripped = script.strip()
    if not stripped.startswith("```"):
        return script
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return ""
    body = stripped[first_newline + 1 :].rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip("\n")
    return body


def run_python_in_sandbox_impl(script: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    """Writes `script` to a file in this thread's sandbox, then runs it
    with `python3 <path>` — see run_python_in_sandbox's own tool
    docstring (app/domains/{ops,support,sales}/tools.py) for WHY this
    exists as a separate tool from run_command_in_sandbox: `script`
    reaches OpenSandbox's own `file_write` as a plain string argument,
    never passed through a shell at all, so it can contain any quotes,
    apostrophes, or newlines without needing the model to get shell
    escaping right — the single most common way run_command_in_sandbox
    calls failed live throughout this app's own development (a
    `python -c '...'` one-liner whose own quotes collide with the
    shell's), confirmed via repeated real Langfuse traces, not a
    one-off."""
    script = _strip_markdown_fence(script)
    sandbox_id = get_or_create_sandbox_id(raw, thread_id)
    _call_raw_tool(
        raw,
        "file_write",
        sandbox_id=sandbox_id,
        path=_RUN_PYTHON_SCRIPT_PATH,
        content=script,
        connect_if_missing=True,
    )
    execution = _call_raw_tool(
        raw,
        "command_run",
        sandbox_id=sandbox_id,
        command=f"python3 {_RUN_PYTHON_SCRIPT_PATH}",
        connect_if_missing=True,
    )
    return _format_execution(execution)


def read_sandbox_file_impl(path: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    # DISCLOSED, PRE-EXISTING third-party gap, found live while adding
    # sandbox tools to two more domains — not introduced by that work, and
    # not something this app's own code can fix: `file_read` reliably
    # 404s ("file not found") on a file THIS SAME PROCESS just wrote with
    # `write_sandbox_file`, confirmed still genuinely present on disk via
    # a real `command_run` (`ls -la <path>`) run immediately after —
    # reproduced with both relative and absolute paths. `write_sandbox_file`
    # and `command_run` are both independently verified working correctly;
    # only `file_read` (opensandbox-mcp==0.1.1 / opensandbox-server==0.2.3)
    # is affected. No prior test in this app ever exercised a real
    # write-then-read round trip live (only mocked) until this was found.
    # Workaround, not a fix: every domain's own `run_command_in_sandbox`
    # docstring now tells the model to use `cat <path>` instead of this
    # tool when it needs a file's contents back.
    sandbox_id = get_or_create_sandbox_id(raw, thread_id)
    result = _call_raw_tool(raw, "file_read", sandbox_id=sandbox_id, path=path, connect_if_missing=True)
    return result.get("content", "")


def write_sandbox_file_impl(path: str, content: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    sandbox_id = get_or_create_sandbox_id(raw, thread_id)
    _call_raw_tool(raw, "file_write", sandbox_id=sandbox_id, path=path, content=content, connect_if_missing=True)
    return f"Wrote {path!r} to the sandbox."
