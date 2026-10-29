"""The docs name files and functions (`app/agent/runtime/stream.py::_run_graph_stream`); a rename must not leave them lying.

Nine of the names in these docs had drifted by the time anyone looked: `builtin.py::run_subagent_impl`, a function moved to
its own module in #167, among them. A reader (or an agent) follows such a pointer and finds nothing, or worse, trusts a
description of code that no longer exists. This checks the repository's own docs (not code comments, which name fixture paths on
purpose); the few references that are meant to point at something gone are listed below, each with the reason.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCS = [
    "GRAPH_PATTERNS.md", "README.md", "CLAUDE.md", "COST_GOVERNANCE.md", "ENGINEERING_PATTERNS.md",
    "WORKER_CONCURRENCY.md", "BILLING_INTEGRATION.md", "ARCHITECTURE.md",
    *sorted(str(p.relative_to(REPO)) for p in (REPO / ".claude" / "rules").glob("*.md")),
]
# (document, reference) pairs that are MEANT not to resolve.
INTENTIONAL = {
    ("CLAUDE.md", "tests/core/test_tool_idempotency.py::test_name"),  # the command example's placeholder
    ("COST_GOVERNANCE.md", "app/agent/usage_ledger.py"),  # the retired module, in text that says it is retired
    ("COST_GOVERNANCE.md", "scripts/usage_ledger_sweep.py"),  # likewise
}
REFERENCE = re.compile(r"(?<![\w/.-])((?:app|scripts|tests|packages)/[\w./-]+?\.py)(?:::(\w+))?")


def _names_defined_in(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def _stale() -> list[str]:
    stale = []
    for doc in DOCS:
        text = (REPO / doc).read_text()
        for match in REFERENCE.finditer(text):
            ref, name = match.group(1), match.group(2)
            full = f"{ref}::{name}" if name else ref
            if (doc, full) in INTENTIONAL or (doc, ref) in INTENTIONAL:
                continue
            line = text.count("\n", 0, match.start()) + 1
            target = REPO / ref
            if not target.exists():
                stale.append(f"{doc}:{line}  {ref} does not exist")
            elif name and name not in _names_defined_in(target):
                stale.append(f"{doc}:{line}  {ref} has no `{name}`")
    return stale


def test_every_file_and_function_the_docs_name_still_exists():
    assert _stale() == []


@pytest.mark.parametrize(("doc", "ref"), sorted(INTENTIONAL))
def test_an_intentional_exception_is_still_needed(doc, ref):
    """An entry nobody needs any more would hide a real stale reference to the same name."""
    path = ref.split("::")[0]
    assert (doc, ref) in INTENTIONAL
    assert ref in (REPO / doc).read_text(), f"{doc} no longer mentions {ref}: drop it from INTENTIONAL"
    if "::" not in ref:
        assert not (REPO / path).exists(), f"{path} exists again: drop it from INTENTIONAL"
