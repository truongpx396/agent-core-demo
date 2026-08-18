"""Shared test fixtures.

`mock_search_docs` is autouse: no test in this suite should reach a live
Qdrant/embeddings backend just because it happens to invoke
`retrieve_context` on its way through the graph via build_graph()'s default
GraphDeps. Tests that care about a specific retrieved value inject their
own fake instead — via `GraphDeps(search_docs=fake)` (build_graph) or
`graph.make_retrieve_context_node(fake)` (node-level) — which simply
bypasses this default.

`TEST_CTX`: a valid SecurityCtx (app/security.py) every test that drives a
turn through validate_input needs — route_after_validation fails closed
without one (see graph.py). Each test file imports it directly
(`from tests.conftest import TEST_CTX`) rather than via a pytest fixture,
since most call sites need it as a plain value inside a hand-built
config/state dict, not injected as a test function parameter.
"""
import pytest

from app import graph

TEST_CTX = {"tenant": "acme", "principal": "test-user", "claims": {}}


@pytest.fixture(autouse=True)
def mock_search_docs(monkeypatch):
    monkeypatch.setattr(graph, "_default_search", lambda query, ctx=None: "")
