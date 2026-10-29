"""The Ecorp domain's use cases: what `add_note`, `remember` and `query_employees` DO, behind the tool layer.

Each function takes the `SecurityCtx` the tool layer already validated and scopes by its `tenant` (and `principal` for memories); none is ever handed a
tenant by the model. `tools.py` is the tool layer (argument schemas, `ctx_or_refuse` / `idempotent()` wrapping); `.claude/rules/side-effect-tools.md` is the
checklist both follow. WHERE the data goes is two ports, `KnowledgeBase` and `EmployeeDirectory` (`app/ports/ecorp.py`; the adapter is `store.py`).

## Cross-session memory

Memory is written only by `remember_impl`; nothing extracts facts from turn text automatically, since whatever writes memory decides what replays into
every future prompt. Recall is automatic (`gather_context` in `app/agent/tools/builtin.py`, alongside document retrieval) and re-filtered against *current*
ctx every call (`Policy.lower(ctx, "memories")`, scoped to `tenant` AND `owner`), never trusted from a prior snapshot. A recalled memory is framed like a
retrieved document (untrusted, delimited) because a poisoned memory recurs every turn until removed (`qdrant_store.delete_by_filter`; deliberately not
agent-facing).
"""
import uuid
from datetime import UTC, datetime
from enum import Enum

from app.agent.tools.builtin import Topic
from app.core.security import SecurityCtx
from app.ports.ecorp import employee_directory, knowledge_base

# Length budget for add_note/remember: bounds the BM25 sparse-embedding cost
# per write (embed_sparse has no internal truncation, unlike the ml-service
# ONNX models — see sparse_vector_or_none) and keeps a note/memory citable
# in a prompt without ballooning it.
MAX_NOTE_TITLE_CHARS = 200
MAX_NOTE_CONTENT_CHARS = 4000
MAX_MEMORY_CONTENT_CHARS = 2000

# Fixed, arbitrary namespace for uuid5's content-addressed point ids below —
# same "any constant works, it just must never change once chosen" rule as
# app/ingestion/ingestor.py's own _POINT_ID_NAMESPACE (a distinct constant,
# not reused, so the two id spaces can never collide even on an identical
# tool_call_id/content coincidence).
TOOL_POINT_ID_NAMESPACE = uuid.UUID("3a9e5f2c-8b1d-4c6a-9e3f-2d7b6a1c8f0e")


def tool_call_point_id(tool_call_id: str) -> str:
    """A Qdrant point id derived from `tool_call_id` alone, not a random
    draw — closes the same duplicate-write gap `idempotent()`'s own
    tool_call_dedup claim narrowly accepts as a race (see that module's
    docstring): if `add_note`/`remember`'s `fn()` runs twice for the SAME
    tool_call_id (the accepted crash-window race, or any future caller
    that reopens it), `qdrant_store.upsert`'s upsert-by-id semantics
    silently overwrite the first point instead of creating a second one.
    Unlike `ingestor.py::_content_point_id`, no content hash is folded in
    here — a tool_call_id is already a fresh, unique id per LLM decision
    (see app/core/tool_idempotency.py's own docstring), so there's nothing else
    worth keying on.
    """
    return uuid.uuid5(TOOL_POINT_ID_NAMESPACE, tool_call_id).hex




async def add_note_impl(
    title: str, content: str, topic: Topic, ctx: SecurityCtx, tool_call_id: str
) -> str:
    """Store one new note in the knowledge base.

    Fixed, single-purpose write: the only variables are the three typed fields above (plus `ctx`, never model-visible). The point id is derived from
    `tool_call_id` (`tool_call_point_id`), not a random draw: this can still only ever *add* a point (never overwrite or target an existing one by guessing
    its id, since a caller never controls `tool_call_id`), but a genuine re-run under the SAME tool_call_id upserts onto the same point instead of
    duplicating it.
    """
    await knowledge_base.get().add_note(
        point_id=tool_call_point_id(tool_call_id), title=title, content=content, topic=topic.value, tenant=ctx["tenant"]
    )
    return f"Note '{title}' added to the {topic.value} knowledge base."


async def remember_impl(content: str, ctx: SecurityCtx, tool_call_id: str) -> str:
    """Store one memory, owned by ctx["principal"] within ctx["tenant"]: the only place a memory gets written (see the module docstring's
    "Cross-session memory" section).

    `created_at` (UTC) is what `Policy.lower`'s retention filter (app/core/security.py, GRAPH_PATTERNS.md pattern 33) and
    `app/retrieval/memory.py::delete_memories` both read: stamped once, here, never caller-supplied. The point id is `tool_call_id`-derived
    (`tool_call_point_id`, same reasoning as `add_note_impl`), so a genuine re-run under the same tool_call_id upserts onto the same memory instead of
    duplicating it.
    """
    await knowledge_base.get().add_memory(
        point_id=tool_call_point_id(tool_call_id),
        content=content,
        tenant=ctx["tenant"],
        owner=ctx["principal"],
        created_at=datetime.now(UTC),
    )
    return "Remembered."


class Department(str, Enum):
    """The only departments that exist — same closed-vocabulary approach
    as `Topic`, and for the same reason: `query_employees` narrows to
    exactly what's askable, never a free-form filter the model writes."""

    engineering = "Engineering"
    support = "Support"
    sales = "Sales"


async def query_employees_impl(
    department: Department | None, name_contains: str | None, ctx: SecurityCtx
) -> str:
    cap = TOOL_RESULT_CAPS["query_employees"]
    rows = await employee_directory.get().query_employees(
        tenant=ctx["tenant"],
        department=department.value if isinstance(department, Department) else department,
        name_contains=name_contains,
        limit=cap + 1,  # +1: enough to detect "more than cap exist" without a second query
    )
    if not rows:
        return "No matching employees found."

    truncated = len(rows) > cap
    rows = rows[:cap]
    lines = [
        f"- {r['name']} — {r['title']}, {r['department']} (hired {r['hired_on']})"
        for r in rows
    ]
    if truncated:
        # Marked, never silently shortened — see TOOL_RESULT_CAPS's comment.
        lines.append(f"[truncated: showing the first {cap} matches; more exist]")
    return "\n".join(lines)


# --- Tool result-size declarations -------------------------------------------
# Per-tool cap (query_employees has no inherent row limit) rather than one
# global constant, since the right cap is a property of the tool. A capped
# result is always MARKED as truncated (see query_employees_impl) — never
# silently shortened.
TOOL_RESULT_CAPS: dict[str, int] = {
    "query_employees": 20,
}
