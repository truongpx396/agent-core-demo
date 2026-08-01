"""LangGraph tools the agent can call.

- `search_docs`: retrieval from Qdrant, with optional metadata (topic) filter.
- `calculator`: a second tool so the agent must *choose* which to call.
"""
import ast
import operator

from langchain_core.tools import tool

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


@tool
def search_docs(query: str, topic: str | None = None) -> str:
    """Search the knowledge base. Optionally filter by topic
    (one of: langgraph, qdrant, company)."""
    hits = qdrant_store.search(embed_text(query), topic=topic, k=3)
    if not hits:
        return "No relevant documents found."
    return "\n".join(f"- {h.payload['text']}" for h in hits)


@tool
def calculator(expression: str) -> str:
    """Evaluate a basic arithmetic expression, e.g. '21 * 2'."""
    try:
        return str(_safe_eval(ast.parse(expression, mode="eval").body))
    except Exception:  # noqa: BLE001
        return f"Could not evaluate: {expression!r}"


TOOLS = [search_docs, calculator]
