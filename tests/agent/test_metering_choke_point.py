"""A chat-model call that does not go through `metering.metered_invoke` is a call the billing meter
cannot see: that is how follow-up suggestions and history compaction came to spend tokens that no
ledger, dollar ceiling or credit balance counted (specs/010 research G1).

This is a ratchet, not a hope. It reads the source (the AST, so a docstring that merely mentions
`llm.ainvoke(...)` does not count) and lists every chat-model call outside the choke point:

  * a NEW call site fails the test, so a hand-rolled `llm.ainvoke` cannot slip in unmetered;
  * an exemption listed below that no longer exists fails it too, so the list can only shrink.

The list started with four known sites (follow-up suggestions, history compaction and the two cron
scripts: specs/010 research G1) and is now empty. It stays, as an explicit and reviewable place to
record a deliberate exception, rather than being deleted.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Empty: every chat-model call goes through the choke point. An exemption added here is a call the
# billing meter cannot see, so it needs a reason that is written down and a plan to remove it.
KNOWN_UNMETERED: set[str] = set()
CHOKE_POINT = "app/agent/metering.py"


def _is_chat_model_call(node: ast.AST) -> bool:
    """`<name>.ainvoke(...)` where the receiver is a bare name that is a chat model client
    (`llm`, `nested_llm`, `chat`, ...). A compiled graph's `graph.ainvoke` is not a model call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "ainvoke"
        and isinstance(node.func.value, ast.Name)
        and any(word in node.func.value.id.lower() for word in ("llm", "chat"))
    )


def _files_with_a_chat_model_call() -> set[str]:
    found = set()
    for root in ("app", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if any(_is_chat_model_call(node) for node in ast.walk(tree)):
                found.add(path.relative_to(REPO).as_posix())
    return found


def test_every_chat_model_call_is_metered_except_the_known_unrouted_ones():
    found = _files_with_a_chat_model_call() - {CHOKE_POINT}

    assert found == KNOWN_UNMETERED, (
        f"new unmetered call site(s): {sorted(found - KNOWN_UNMETERED)}; "
        f"stale entries to delete from KNOWN_UNMETERED: {sorted(KNOWN_UNMETERED - found)}. "
        "Route a model call through app/agent/metering.py::metered_invoke."
    )


def test_the_choke_point_itself_is_where_the_agent_nodes_call_goes():
    """Guards the guard: if the scanner stopped recognising calls it would pass vacuously."""
    assert CHOKE_POINT in _files_with_a_chat_model_call()
    assert "app/agent/graph_agent_node.py" not in _files_with_a_chat_model_call()
