"""Pure helper logic behind the ops domain's three sandbox tools —
run_command_in_sandbox, read_sandbox_file, write_sandbox_file
(app/domains/ops/tools.py, the only module that wraps these as `@tool`
objects / does ctx checks) — built on top of OpenSandbox's raw MCP catalog
(app/domains/sandbox_tools.py, GRAPH_PATTERNS.md pattern 50) instead of
exposing that catalog to the LLM directly, the way app/domains/ops/domain.py
originally did.

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
tests/domains/ops/test_sandbox_session.py.
"""
import json
import logging

from langchain_core.tools import BaseTool

from app.core.config import OPS_SANDBOX_IMAGE, OPS_SANDBOX_TTL_SECONDS
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


def load_raw_sandbox_tools() -> dict[str, BaseTool]:
    """OpenSandbox's raw MCP tool catalog as a {name: tool} lookup, empty
    if opensandbox-mcp isn't installed/reachable (see
    app/domains/sandbox_tools.py's own docstring for why that degrade is
    safe to rely on eagerly). Called once, at app/domains/ops/tools.py's own
    import time — ops/tools.py only builds/exposes the three sandbox tools
    below when this comes back non-empty."""
    raw_tools, _capabilities = load_sandbox_tools()
    return {t.name: t for t in raw_tools}


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


def _find_existing_sandbox_id(raw: dict[str, BaseTool], thread_id: str) -> str | None:
    """Looks up a RUNNING sandbox already tagged for this thread — see
    module docstring for why this is a server-side sandbox_list metadata
    filter, not a local cache. Returns None (not found, or the lookup
    itself failed) rather than raising: a failed lookup should fall
    through to creating a fresh sandbox, not abort the whole call."""
    try:
        result = _call_raw_tool(
            raw, "sandbox_list", filter={"metadata": {SANDBOX_METADATA_KEY: thread_id}, "states": ["RUNNING"]}
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
    existing = _find_existing_sandbox_id(raw, thread_id)
    if existing:
        return existing
    created = _call_raw_tool(
        raw,
        "sandbox_create",
        image=OPS_SANDBOX_IMAGE,
        metadata={SANDBOX_METADATA_KEY: thread_id},
        timeout_seconds=OPS_SANDBOX_TTL_SECONDS,
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


def read_sandbox_file_impl(path: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    sandbox_id = get_or_create_sandbox_id(raw, thread_id)
    result = _call_raw_tool(raw, "file_read", sandbox_id=sandbox_id, path=path, connect_if_missing=True)
    return result.get("content", "")


def write_sandbox_file_impl(path: str, content: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    sandbox_id = get_or_create_sandbox_id(raw, thread_id)
    _call_raw_tool(raw, "file_write", sandbox_id=sandbox_id, path=path, content=content, connect_if_missing=True)
    return f"Wrote {path!r} to the sandbox."
