"""Shared test fixtures.

`mock_search_docs` is autouse: no test in this suite should reach a live
Qdrant/embeddings backend just because it happens to invoke
`retrieve_context` on its way through the graph. Tests that care about a
specific retrieved value monkeypatch `app.graph.search_docs` themselves,
which simply overrides this default for the duration of that test.
"""
import pytest

from app import graph


@pytest.fixture(autouse=True)
def mock_search_docs(monkeypatch):
    monkeypatch.setattr(graph, "search_docs", lambda query, topic=None: "")
