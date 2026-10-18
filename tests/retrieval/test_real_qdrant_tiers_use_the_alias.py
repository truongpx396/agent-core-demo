"""A test that talks to a REAL Qdrant must address the collection by its alias, never by the bare logical name.

Since specs/014 the data lives in a physical collection `<name>-<identity hash>` reached through the alias `<name>-live`; a collection
literally named `<name>` exists only for a legacy deployment that has not been re-embedded. A test that scrolls, counts or deletes
`COLLECTION` against a real server therefore passes on a hand-built fixture and gets a 404 on a fresh stack. That happened three times
while this feature was written (two integration tests, then the live tier's memory fixture, which only CI's `test-live` could see), and
the live tier needs Ollama and a browser, so it is exactly the tier nobody runs by hand.

This is a static check, not a substitute for those tiers: it forbids passing the bare `COLLECTION` constant as an argument in a file
that opens a real Qdrant connection. The helper tests that use `QdrantClient(":memory:")` have no alias and are exempt.
"""
import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent.parent
REAL_QDRANT_TIERS = [*sorted((TESTS / "live").glob("*.py")), *sorted((TESTS / "integration").glob("*.py"))]


def _passes_the_bare_constant(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for arg in [*node.args, *(kw.value for kw in node.keywords)]:
            if isinstance(arg, ast.Name) and arg.id == "COLLECTION" or isinstance(arg, ast.Attribute) and arg.attr == "COLLECTION":
                lines.append(arg.lineno)
    return lines


def test_the_checker_flags_the_bare_constant_and_not_the_alias():
    assert _passes_the_bare_constant("memory_ids(client, COLLECTION)\nx(c, collection_name=config.COLLECTION)") == [1, 2]
    assert _passes_the_bare_constant("memory_ids(client, live_name())\nCOLLECTION = 'docs'") == []


@pytest.mark.parametrize("path", REAL_QDRANT_TIERS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_a_real_qdrant_tier_never_passes_the_bare_collection_name(path):
    source = path.read_text()
    if ":memory:" in source:
        pytest.skip("an in-memory client has no aliases")
    assert _passes_the_bare_constant(source) == [], f"{path.name} passes the logical COLLECTION to a call; use live_name() (the alias)"
