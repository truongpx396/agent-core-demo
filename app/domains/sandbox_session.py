"""Pure helper logic behind each domain's three sandbox tools
(run_command_in_sandbox, read_sandbox_file, write_sandbox_file), wrapped
per domain in app/domains/{ops,support,sales}/tools.py — built on
OpenSandbox's raw MCP catalog (app/domains/sandbox_tools.py, pattern 50)
instead of exposing that catalog to the LLM directly.

Domain-agnostic on purpose: nothing below is ops-specific, hence living
under app/domains/ rather than app/domains/ops/.

## Why this exists
Handing a small local model (qwen2.5:3b) OpenSandbox's raw ~19-tool
catalog (stateful create -> connect -> run, nested schemas) produced
reproducible failures: it hallucinated a sandbox_id, then looped on
sandbox_connect with no args until the no-progress safety net (pattern 34)
cut it short. A prompt-only fix didn't help — a real instruction-following
ceiling, not a prompting problem. Every other tool in this app is 1-3 flat
fields; this module gives sandbox access the same shape — one flat string
arg per tool, full create/reuse/connect lifecycle handled here in code.
The model never sees a sandbox_id or calls sandbox_create/sandbox_connect.

## Per-thread reuse, server-side
Each thread gets ONE sandbox, tagged `{SANDBOX_METADATA_KEY: thread_id}`
at creation and found again via sandbox_list's metadata filter — a
server-side lookup, not a process-local dict, so it stays correct across
this app's horizontally-scaled workers (pattern 43): a resume picked up by
a different worker than the one that created the sandbox still finds it.

`connect_if_missing=True` is passed on every command_run/file_read/
file_write call unconditionally: `load_remote_tools` spawns a fresh
opensandbox-mcp subprocess per call (app/mcp/client.py), so the bridge's
local sandbox registry never persists between calls anyway — every call
after the creating one would otherwise 404 regardless of sandbox_id.

## Response shapes
Confirmed live against a real opensandbox-server (port 8090, `make
sandbox-up`):
- sandbox_create -> `{"sandbox_id": ..., "info": {...}}`
- sandbox_list -> `{"sandbox_infos": [{"id": ..., "status": {"state": "RUNNING", ...}, "metadata": {...}, ...}], "pagination": {...}}`
- command_run -> Execution: `{"exit_code": int|None, "logs": {"stdout": [{"text": ...}], "stderr": [{"text": ...}]}, ...}`
- file_read -> `{"path": ..., "content": ...}`
- file_write -> `{"status": "written"}`
Tested against these shapes in tests/domains/test_sandbox_session.py.
"""
import json
import logging
import re

from langchain_core.tools import BaseTool

from app.core.config import SANDBOX_IMAGE, SANDBOX_TTL_SECONDS
from app.domains.sandbox_tools import load_sandbox_tools

logger = logging.getLogger(__name__)

SANDBOX_METADATA_KEY = "agent_core_thread"
SANDBOX_CALL_TIMEOUT_SECONDS = 60  # own budget vs TOOL_TIMEOUT_SECONDS:
# command_run can legitimately run longer than a plain HTTP round trip.
# Was briefly bumped to 150s after prod timeouts traced to
# `ConnectionConfig.use_server_proxy=False` making opensandbox-mcp try to
# reach each sandbox at its unreachable Docker bridge IP (44s+ retries);
# fixed via scripts/opensandbox_mcp_bridge.py, so 60s is generous again.


class SandboxCallFailed(Exception):
    """A raw OpenSandbox MCP tool call errored, or returned a shape this
    module couldn't parse — an expected outcome that propagates to the
    calling tool's normal exception handling (graph.py's
    handle_tool_errors), not a bug that should surface as a raw
    traceback."""


_raw_sandbox_tools_cache: dict[str, BaseTool] = {}


async def load_raw_sandbox_tools() -> dict[str, BaseTool]:
    """OpenSandbox's raw MCP tool catalog as a {name: tool} lookup, empty
    if opensandbox-mcp isn't installed/reachable. Called per-call by each
    domain's tools.py wrapper, NOT at module import time.

    Self-healing: a successful (non-empty) result is cached for the
    process's life; an empty result is never cached, so calls made while
    opensandbox-mcp is unreachable keep retrying instead of latching onto
    a permanent empty cache. Fixes a real bug: this used to cache a `None`
    sentinel at import time, so a dev server that booted before
    opensandbox-server finished starting made every sandbox tool invisible
    for that process's whole life, with the model hallucinating a
    nonexistent tool name to route around the gap (Langfuse trace
    806125c9). Every caller shares the same dict; nothing mutates it."""
    global _raw_sandbox_tools_cache
    if not _raw_sandbox_tools_cache:
        raw_tools, _capabilities = await load_sandbox_tools()
        _raw_sandbox_tools_cache = {t.name: t for t in raw_tools}
    return _raw_sandbox_tools_cache


async def _call_raw_tool(raw: dict[str, BaseTool], name: str, **kwargs) -> dict:
    """Calls one of OpenSandbox's raw MCP tools by name, parses the result
    into a dict. Uses `.ainvoke` (the standard Runnable interface, not the
    StructuredTool-specific `.func`/`.coroutine`), since
    `_wrap_remote_tool` (app/mcp/client.py) gives each tool a native async
    coroutine and every caller here is already `async def`. Raises
    SandboxCallFailed on a remote tool error or an unparseable/non-dict
    response, rather than silently returning a wrong shape."""
    raw_text = await raw[name].ainvoke(kwargs)
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
    """OpenSandbox's metadata VALUE rules: <=63 chars, start/end
    alphanumeric, only alphanumeric/-/_/. in between. Real thread_ids don't
    satisfy this — telegram.py's `_thread_id_for_chat` returns
    `f"telegram:{chat_id}"`, and that colon alone failed every
    sandbox_create/sandbox_list call with SANDBOX::INVALID_METADATA_LABEL.
    Sanitized here rather than changing thread_id's format at the source,
    since thread_id is also the checkpointer key and Langfuse metadata —
    this constraint is OpenSandbox's alone. Sanitized-value collisions are
    theoretical at this app's scale."""
    sanitized = _INVALID_METADATA_CHARS.sub("-", thread_id)[:63].strip("_-.")
    return sanitized or "thread"


async def _find_existing_sandbox_id(raw: dict[str, BaseTool], sandbox_metadata_value: str) -> str | None:
    """Looks up a RUNNING sandbox already tagged for this thread (see
    module docstring). Returns None on not-found or lookup failure rather
    than raising, so a failed lookup falls through to creating a fresh
    sandbox instead of aborting the call."""
    try:
        result = await _call_raw_tool(
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


async def get_or_create_sandbox_id(raw: dict[str, BaseTool], thread_id: str) -> str:
    """Reuse this thread's existing sandbox if sandbox_list finds one,
    else create a fresh one tagged for it. Raises SandboxCallFailed if
    creation fails (e.g. opensandbox-server unreachable, pattern 50) —
    nothing to fall back to."""
    sandbox_metadata_value = _sanitize_thread_id_for_metadata(thread_id)
    existing = await _find_existing_sandbox_id(raw, sandbox_metadata_value)
    if existing:
        return existing
    created = await _call_raw_tool(
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
    """Execution -> plain-text summary (exit code + stdout + stderr,
    stderr only when non-empty), not raw JSON — keeps the result as simple
    for the model as any other tool's."""
    exit_code = execution.get("exit_code")
    logs = execution.get("logs") or {}
    stdout = "".join(m.get("text", "") for m in (logs.get("stdout") or []))
    stderr = "".join(m.get("text", "") for m in (logs.get("stderr") or []))
    lines = [f"exit code: {exit_code}"]
    lines.append(f"stdout:\n{stdout}" if stdout else "stdout: (empty)")
    if stderr:
        lines.append(f"stderr:\n{stderr}")
    return "\n".join(lines)


async def run_command_in_sandbox_impl(command: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    sandbox_id = await get_or_create_sandbox_id(raw, thread_id)
    execution = await _call_raw_tool(
        raw, "command_run", sandbox_id=sandbox_id, command=command, connect_if_missing=True
    )
    return _format_execution(execution)


# Fixed path, overwritten fresh on every call — no unique name needed.
# Leading underscore is just a naming convention, not real privacy.
_RUN_PYTHON_SCRIPT_PATH = "_run_python_in_sandbox.py"


def _strip_markdown_fence(script: str) -> str:
    """Strip a markdown code fence (```python\\n...\\n```) if the model
    wrapped its script in one — a real recurring mistake, since a fenced
    block is how the model normally shows code in prose. Only activates
    when the script STARTS with ``` (three backticks can't legally begin
    Python, so it's an unambiguous signal); anything else passes through
    untouched. Handles both a closing ``` on its own line and one glued
    directly onto the last code line."""
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


async def run_python_in_sandbox_impl(script: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    """Writes `script` to a file in this thread's sandbox, then runs it
    with `python3 <path>`. Exists as a separate tool from
    run_command_in_sandbox because `script` reaches `file_write` as a
    plain string, never through a shell — avoids the shell-escaping
    failures (`python -c '...'` colliding with its own quotes) that were
    the most common run_command_in_sandbox failure mode in practice."""
    script = _strip_markdown_fence(script)
    sandbox_id = await get_or_create_sandbox_id(raw, thread_id)
    await _call_raw_tool(
        raw,
        "file_write",
        sandbox_id=sandbox_id,
        path=_RUN_PYTHON_SCRIPT_PATH,
        content=script,
        connect_if_missing=True,
    )
    execution = await _call_raw_tool(
        raw,
        "command_run",
        sandbox_id=sandbox_id,
        command=f"python3 {_RUN_PYTHON_SCRIPT_PATH}",
        connect_if_missing=True,
    )
    return _format_execution(execution)


async def read_sandbox_file_impl(path: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    # Known third-party bug: `file_read` (opensandbox-mcp==0.1.1 /
    # opensandbox-server==0.2.3) reliably 404s on a file this same process
    # just wrote via write_sandbox_file, even though it's genuinely on disk
    # (confirmed via `command_run`'s `ls -la`). write_sandbox_file and
    # command_run are unaffected. Workaround: each domain's
    # run_command_in_sandbox docstring tells the model to use `cat <path>`
    # instead of this tool.
    sandbox_id = await get_or_create_sandbox_id(raw, thread_id)
    result = await _call_raw_tool(raw, "file_read", sandbox_id=sandbox_id, path=path, connect_if_missing=True)
    return result.get("content", "")


async def write_sandbox_file_impl(path: str, content: str, thread_id: str, raw: dict[str, BaseTool]) -> str:
    sandbox_id = await get_or_create_sandbox_id(raw, thread_id)
    await _call_raw_tool(
        raw, "file_write", sandbox_id=sandbox_id, path=path, content=content, connect_if_missing=True
    )
    return f"Wrote {path!r} to the sandbox."
