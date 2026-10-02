# Contract: The Sandbox Tools

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §1–§3, §7](../data-model.md) | **Constitution**: Principle I (tenant isolation), Principle II (approval), Principle IV (no blind retry)

**Status**: Retrospective — `app/domains/sandbox_session.py`, `app/domains/sandbox_tools.py`, the per-product wrappers in `app/domains/{support,ops,sales}/tools.py`, `scripts/opensandbox_mcp_bridge.py`, `docker/opensandbox-server.*`;
tests in `tests/domains/test_sandbox_session.py`, `test_sandbox_tools.py`, each product's `test_domain.py`, and the manual tier `tests/live/test_*sandbox*_live.py`.
**Not tested: that two conversations cannot reach one another's sandbox (B24); the images (B23); sandbox output size (A3).**

## Audience

The **assistant** (these four tools are its whole sandbox interface) and an **operator** deploying the sandbox service.

## The four tools (each product has its own set)

| Tool | Arguments | Does | Returns |
|------|-----------|------|---------|
| `run_command_in_sandbox` | `command` | runs a shell command in this conversation's sandbox | `exit code: n` + `stdout:` (+ `stderr:` if any) |
| `run_python_in_sandbox` | `script` | writes the script to a fixed file (a wrapping fence stripped), then runs `python3 <file>` | the same |
| `read_sandbox_file` | `path` | reads a file | its content — **unreliable right after a write against the pinned release; use `cat` (A6)** |
| `write_sandbox_file` | `path`, `content` | writes a file | `Wrote '<path>' to the sandbox.` |

- All four: **flat string arguments only**; the assistant never sees, chooses or tracks a sandbox id; **`outward`** capability → the turn **pauses for approval** every time; wrapped by the call-id protection; bounded by a 60 s soft timeout.
- **Arguments are not length-bounded (A3)** and results are returned whole (A3).
- The assistant is told "do not `pip install`; numpy and pandas are available" and "no network access" — a claim about the third-party default that is **unverified** (A2).

## The session lifecycle (in code, invisible to the assistant)

1. Compute the **conversation tag** from the thread id (data-model §1 — rewritten, not injective: **B24**).
2. `sandbox_list` filtered by that tag and `RUNNING` → reuse the first hit; a lookup failure logs `sandbox_list_failed` and **falls through**.
3. Otherwise `sandbox_create(image, tag, 1,800 s)`; a failure, or a reply without `sandbox_id`, raises `SandboxCallFailed`.
4. Run the operation with `connect_if_missing=True` (every call is a fresh bridge process).
5. The sandbox is **never deleted** by the app; it expires 30 minutes after creation and its contents are lost silently.

## Failure behavior

| Situation | What the assistant sees | Retried? |
|-----------|-------------------------|----------|
| Sandbox service unreachable / bridge missing | `OpenSandbox is not reachable right now (opensandbox-mcp/opensandbox-server may still be starting, or the sandbox profile isn't running) — try again in a moment.` | the **catalogue listing** once after 1 s, then fails fast (3 failures → 30 s breaker); never a command |
| A remote error on a call | `SandboxCallFailed: Remote tool error: …` (scrubbed) → the standard tool-error message | **no** |
| An unparseable or non-object reply | `SandboxCallFailed` naming the tool and the first 300 characters | no |
| The 60 s soft timeout | a tool timeout → for an outward tool, the verify-don't-retry message (feature 003) | **no** |
| **A containerized deployment** | the "not reachable" message **permanently** (B23: the bridge script is not in the image) | n/a |

## Deployment requirements

- The sandbox service (`opensandbox-server`, own `sandbox` compose profile) must be reachable at `OPENSANDBOX_MCP_DOMAIN` with `OPENSANDBOX_API_KEY`; the host needs `opensandbox-mcp` and the bridge script; the sandbox image must exist (`make sandbox-build`).
- The control-plane container holds the host's container-runtime socket **read-write** (disclosed; opt-in profile).
- **Not in any image**: `scripts/opensandbox_mcp_bridge.py` (B23).

## Invariants a change must preserve

1. Every sandbox tool is `outward`; none is reachable from a scheduled or unattended path.
2. A command or a file write is **never retried** automatically.
3. The assistant never sees a sandbox id.
4. A catalogue that cannot be loaded degrades the tools; it never stops a product being built, and an empty result is never cached.
5. **A conversation's sandbox is reachable from that conversation only** *(not yet true — B24)*.
