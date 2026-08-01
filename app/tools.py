"""LangGraph tools the agent can call.

- `search_docs`: retrieval from Qdrant, with an optional metadata (topic) filter.
- `calculator`: a second tool so the agent must *choose* which to call.

Both tools declare an explicit **Pydantic `args_schema`**. This is the robust
way to define tool inputs: the schema (enums, descriptions, validators) is what
the LLM sees as the tool's JSON schema, so it constrains what the model can
send and rejects bad arguments loudly instead of failing silently.
"""
import ast
import operator
from enum import Enum

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

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


@tool(args_schema=SearchDocsArgs)
def search_docs(query: str, topic: Topic | None = None) -> str:
    """Search the knowledge base for relevant documents."""
    topic_value = topic.value if isinstance(topic, Topic) else topic
    hits = qdrant_store.search(embed_text(query), topic=topic_value, k=3)
    if not hits:
        return "No relevant documents found."
    return "\n".join(f"- {h.payload['text']}" for h in hits)


@tool(args_schema=CalculatorArgs)
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


TOOLS = [search_docs, calculator]
