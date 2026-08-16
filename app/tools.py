"""LangGraph tools the agent can call.

- `search_docs`: retrieval from Qdrant, with an optional metadata (topic) filter.
- `calculator`: a second tool so the agent must *choose* which to call.
- `add_note`: writes a new note into the Qdrant knowledge base — the one
  *mutating* tool. See `TOOL_CAPABILITIES` below for why it's declared, not
  just implemented, as mutating.

All tools declare an explicit **Pydantic `args_schema`**. This is the robust
way to define tool inputs: the schema (enums, descriptions, validators) is what
the LLM sees as the tool's JSON schema, so it constrains what the model can
send and rejects bad arguments loudly instead of failing silently. `add_note`
leans on this harder than the read-only tools do: its args are a fixed,
closed set (title/content/topic, topic restricted to the existing `Topic`
enum) — there is no free-form query or generated-write path here, deliberately.
A tool that let the model construct its own write target (the equivalent of
letting it generate SQL) would defeat the whole point of gating writes behind
`add_note`'s narrow, reviewable surface — see GRAPH_PATTERNS.md's "fixed
tools, never generated queries" note.
"""
import ast
import concurrent.futures
import operator
import uuid
from enum import Enum
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator
from qdrant_client.models import PointStruct

from app import qdrant_store
from app.embeddings import embed_text

# Whitelisted operators for the safe calculator.
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}

# Safety budget: bound how long any single tool call can block the graph
# (a hung Qdrant/embedding call, or a pathological expression like
# `2**99999999999`, would otherwise stall — or in the exponent case,
# potentially exhaust memory in — the whole turn indefinitely).
TOOL_TIMEOUT_SECONDS = 15
_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="tool-timeout"
)


def _run_with_timeout(func, *args, **kwargs):
    """Run `func` in a worker thread and stop waiting after
    TOOL_TIMEOUT_SECONDS. The raised TimeoutError is caught by ToolNode's
    `handle_tool_errors=_friendly_tool_error` in app/graph.py and turned
    into a message the agent sees on its next turn, same as any other tool
    exception.

    Soft timeout: Python can't forcibly kill the worker thread, so this
    bounds how long the *graph* waits, not how long the call actually runs
    in the background.
    """
    future = _TOOL_EXECUTOR.submit(func, *args, **kwargs)
    try:
        return future.result(timeout=TOOL_TIMEOUT_SECONDS)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(
            f"Tool call exceeded the {TOOL_TIMEOUT_SECONDS}s timeout."
        ) from exc


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):  # numbers
        return node.value
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression")


class Topic(str, Enum):
    """The only topics that exist in the knowledge base."""

    langgraph = "langgraph"
    qdrant = "qdrant"
    company = "company"


class SearchDocsArgs(BaseModel):
    query: str = Field(..., description="Natural-language search query.")
    topic: Topic | None = Field(
        default=None,
        description="Optional filter. Must be one of: langgraph, qdrant, company. "
        "Omit it to search across all topics.",
    )


class CalculatorArgs(BaseModel):
    expression: str = Field(..., description="Arithmetic expression, e.g. '21 * 2'.")

    @field_validator("expression")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("expression must not be empty")
        return v


def _search_docs_impl(query: str, topic: Topic | None) -> str:
    topic_value = topic.value if isinstance(topic, Topic) else topic
    hits = qdrant_store.search(embed_text(query), topic=topic_value, k=3)
    if not hits:
        return "No relevant documents found."
    return "\n".join(f"- {h.payload['text']}" for h in hits)


@tool(args_schema=SearchDocsArgs)
def search_docs(query: str, topic: Topic | None = None) -> str:
    """Search the knowledge base for relevant documents."""
    return _run_with_timeout(_search_docs_impl, query, topic)


def _calculator_impl(expression: str) -> str:
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


@tool(args_schema=CalculatorArgs)
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    return _run_with_timeout(_calculator_impl, expression)


class AddNoteArgs(BaseModel):
    title: str = Field(..., description="Short title for the note.")
    content: str = Field(..., description="The note's text.")
    topic: Topic = Field(
        ..., description="Must be one of: langgraph, qdrant, company."
    )

    @field_validator("title", "content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v


def _add_note_impl(title: str, content: str, topic: Topic) -> str:
    """Embed and upsert one new point into the knowledge base.

    A fixed, single-purpose write: the only variables are the three typed
    fields above, there's no query/filter/target the model constructs
    itself. A fresh UUID id (never a caller-supplied one) means this can
    only ever *add* a point, never overwrite or target an existing one by
    guessing its id — the write surface this tool exposes is exactly
    "append one note," nothing broader.
    """
    text = f"{title}: {content}"
    vector = embed_text(text)
    point = PointStruct(
        id=str(uuid.uuid4()),
        vector=vector,
        payload={"text": text, "topic": topic.value, "title": title},
    )
    qdrant_store.upsert([point])
    return f"Note '{title}' added to the {topic.value} knowledge base."


@tool(args_schema=AddNoteArgs)
def add_note(title: str, content: str, topic: Topic) -> str:
    """Add a new note to the knowledge base so future searches can find it.

    This WRITES to the knowledge base — unlike search_docs/calculator, this
    changes what other users and future turns will see. It is declared
    "mutating" in TOOL_CAPABILITIES, which means app/graph.py's
    should_continue routes it through human_approval every time, regardless
    of whether the caller opted into require_approval.
    """
    return _run_with_timeout(_add_note_impl, title, content, topic)


TOOLS = [search_docs, calculator, add_note]

# --- Tool capability declarations -------------------------------------------
# Every tool declares which "leg" of exposure it adds: read_only (safe to run
# immediately), mutating (writes/changes persisted state), or outward
# (reaches outside the corpus — sends, calls an external service, etc.).
# app/graph.py's should_continue enforces this: a tool_call batch containing
# ANY non-read_only tool is routed through human_approval unconditionally —
# "mandatory," not the opt-in require_approval gate — because a retrieval-
# augmented agent's context is untrusted content on essentially every turn
# (see GRAPH_PATTERNS.md pattern 12): once a run already carries "exposure to
# untrusted content," adding "ability to mutate state" is the second of the
# two legs a run may hold unsupervised, and the third (private/sensitive
# data — not distinguished in this single-tenant demo) is never worth
# gambling on. A tool absent from this mapping defaults to "outward" — fail
# closed, so a new tool added to TOOLS without a capability entry is gated
# rather than silently trusted.
ToolCapability = Literal["read_only", "mutating", "outward"]

TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "search_docs": "read_only",
    "calculator": "read_only",
    "add_note": "mutating",
}
