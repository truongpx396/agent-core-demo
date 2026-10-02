"""A contract test over EVERY non-read-only tool of EVERY domain plugin.

Principles II and IV (mandatory approval; exactly-once side effects) are
non-negotiable, and what makes a write tool safe is three small things it must
each remember to do: refuse when there is no valid tenant/principal context,
route its real work through `idempotent()`, and hand `idempotent()` the injected
`tool_call_id` and its own name. Every tool has behavior tests, but they were
written per behavior — nothing failed if a NEW write tool, or an edit to an
existing one, quietly dropped one of the three. This file does.

It enumerates the tools from the registry rather than from a list kept here, so
a tool added tomorrow is covered the moment it is registered — and, because its
sample arguments must be declared below, adding one forces its author to look at
this checklist (`.claude/rules/side-effect-tools.md`).

What it deliberately does NOT prove: that `idempotent()` itself is exactly-once
(tests/agent/test_tool_idempotency.py), or that each tool's row-level uniqueness
holds at the store (the per-store tests). It pins that every write tool is
*wired* to them.
"""
import sys

import pytest

from app.domains.registry import DOMAINS
from tests.conftest import TEST_CTX

# Valid arguments for each write tool — exactly what the model would pass, minus
# the injected `tool_call_id`. A tool missing from this table fails
# `test_every_write_tool_has_sample_arguments` with instructions.
SAMPLE_ARGS: dict[str, dict] = {
    "add_note": {"title": "Refunds", "content": "30 days.", "topic": "company"},
    "remember": {"content": "prefers email"},
    "create_ticket": {"subject": "Login broken", "description": "Cannot sign in."},
    "add_ticket_comment": {"ticket_id": 1, "comment": "Any update?"},
    "escalate_to_human": {"ticket_id": 1, "reason": "angry customer"},
    "fetch_external_reference": {"url": "https://example.com/status"},
    "log_incident": {"summary": "Checkpoint errors rising"},
    "resolve_incident": {"incident_id": 1, "resolution": "restarted the worker"},
    "post_to_team_channel": {"channel": "ops-alerts", "message": "Queue is backing up"},
    "check_vendor_status_page": {"url": "https://status.example.com"},
    "log_lead_interaction": {"name": "Ada", "contact": "ada@example.com", "notes": "asked for pricing"},
    "schedule_followup": {"contact": "ada@example.com", "due_in_days": 3, "note": "send the quote"},
    "handoff_to_human": {"contact": "ada@example.com", "brief_summary": "wants a demo", "reason": "pricing"},
    "mark_lead_lost": {"contact": "ada@example.com", "reason": "went with a competitor"},
    "enrich_lead_from_website": {"contact": "ada@example.com", "url": "https://example.com"},
    "run_command_in_sandbox": {"command": "echo hi"},
    "run_python_in_sandbox": {"script": "print('hi')"},
    "read_sandbox_file": {"path": "/workspace/out.txt"},
    "write_sandbox_file": {"path": "/workspace/out.txt", "content": "hi"},
}


def _write_tools() -> list[tuple[str, object]]:
    """(`<domain>:<tool>`, tool) for every tool that is not `read_only`. A tool
    a plugin leaves out of its capability mapping counts as `outward` — the
    same default `should_continue` applies — so it is in scope too."""
    found = []
    for domain, (_manifest, plugin) in sorted(DOMAINS.items()):
        capabilities = plugin.tool_capabilities()
        for tool in plugin.tools():
            if capabilities.get(tool.name, "outward") != "read_only":
                found.append((f"{domain}:{tool.name}", tool))
    return found


WRITE_TOOLS = _write_tools()
IDS = [tool_id for tool_id, _ in WRITE_TOOLS]


def _call(tool, config):
    """Invoked the way ToolNode does — as a tool call, so the injected
    `tool_call_id` is filled in and the args are validated by the schema."""
    return tool.ainvoke(
        {"name": tool.name, "args": SAMPLE_ARGS[tool.name], "id": "call-contract-1", "type": "tool_call"},
        config=config,
    )


@pytest.fixture
def spies(monkeypatch):
    """Replaces `idempotent` and `_arun_with_timeout` in every module that
    defines a write tool: the first records how it was called, the second fails
    the test if a tool reaches its real work without going through the first."""
    idempotent_calls: list[dict] = []

    async def spy_idempotent(*, tool_call_id, ctx, config, tool_name, fn):
        idempotent_calls.append({"tool_call_id": tool_call_id, "tool_name": tool_name, "ctx": ctx})
        return "CANNED-RESULT"

    async def forbidden(*args, **kwargs):
        raise AssertionError("a write tool reached its real work without going through idempotent()")

    for module_name in {tool.coroutine.__module__ for _id, tool in WRITE_TOOLS}:
        module = sys.modules[module_name]
        monkeypatch.setattr(module, "idempotent", spy_idempotent, raising=False)
        monkeypatch.setattr(module, "_arun_with_timeout", forbidden, raising=False)
    return idempotent_calls


def test_the_inventory_found_the_write_tools():
    """Guards the enumeration itself: if a refactor made `_write_tools()` come
    back empty, every test below would pass vacuously."""
    names = {tool_id.split(":", 1)[1] for tool_id in IDS}
    assert {"add_note", "create_ticket", "log_incident", "log_lead_interaction", "run_command_in_sandbox"} <= names
    assert len(WRITE_TOOLS) >= 20


def test_every_write_tool_has_sample_arguments():
    missing = sorted({tool_id.split(":", 1)[1] for tool_id in IDS} - SAMPLE_ARGS.keys())
    assert not missing, (
        f"New write tool(s) {missing}: add valid sample arguments to SAMPLE_ARGS in this file, and confirm each "
        "satisfies the checklist in .claude/rules/side-effect-tools.md (capability tier, ctx check, idempotent(), "
        "row-level uniqueness, tenant-scoped SQL, tests)."
    )


@pytest.mark.parametrize("tool_id,tool", WRITE_TOOLS, ids=IDS)
async def test_a_write_tool_refuses_without_a_valid_context_and_does_nothing(spies, tool_id, tool):
    """No ctx means the request never had a security context stamped upstream
    (Principle I) — the tool must refuse BEFORE idempotent() or any store."""
    result = await _call(tool, {"configurable": {}})

    assert "Refused" in str(result.content), f"{tool_id} did not refuse: {result.content!r}"
    assert spies == [], f"{tool_id} reached idempotent() without a valid ctx"


@pytest.mark.parametrize("tool_id,tool", WRITE_TOOLS, ids=IDS)
async def test_a_write_tool_runs_its_work_through_idempotent_with_its_call_id_and_name(spies, tool_id, tool):
    """Principle IV layer one: with a valid ctx the work goes through
    `idempotent()`, keyed by the injected call id and the tool's own name — the
    key a replayed call is recognized by."""
    await _call(tool, {"configurable": {"ctx": TEST_CTX, "thread_id": "contract-thread"}})

    assert len(spies) == 1, f"{tool_id} called idempotent() {len(spies)} times, expected exactly once"
    assert spies[0]["tool_call_id"] == "call-contract-1"
    assert spies[0]["tool_name"] == tool.name
    assert spies[0]["ctx"]["tenant"] == TEST_CTX["tenant"]
