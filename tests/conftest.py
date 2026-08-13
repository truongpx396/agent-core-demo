"""Shared test fixtures.

`mock_search_docs` is autouse: no test in this suite should reach a live
Qdrant/embeddings backend just because it happens to invoke
`retrieve_context` on its way through the graph via build_graph()'s default
GraphDeps. Tests that care about a specific retrieved value inject their
own fake instead — via `GraphDeps(search_docs=fake)` (build_graph) or
`graph.make_retrieve_context_node(fake)` (node-level) — which simply
bypasses this default.
"""
import pytest

from app import graph


@pytest.fixture(autouse=True)
def mock_search_docs(monkeypatch):
    monkeypatch.setattr(graph, "_default_search", lambda query: "")
