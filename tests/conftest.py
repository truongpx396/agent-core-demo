"""Shared test fixtures.

`mock_search_docs`/`mock_semantic_cache` are autouse: no test in this suite
should reach a live Qdrant/Redis/embeddings backend just because it happens
to invoke `retrieve_context`/`check_semantic_cache`/`write_semantic_cache`
on its way through the graph via build_graph()'s default GraphDeps. Tests
that care about a specific retrieved/cached value inject their own fake
instead — via `GraphDeps(search_docs=fake)`/`GraphDeps(cache_get=fake, ...)`
(build_graph) or `graph.make_retrieve_context_node(fake)`/
`graph.make_check_semantic_cache_node(fake)` (node-level) — which simply
bypasses these defaults.

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
    monkeypatch.setattr(graph, "_default_search", lambda query, ctx=None: ("", []))


@pytest.fixture(autouse=True)
def mock_semantic_cache(monkeypatch):
    # Always a miss, and writes are a no-op — a live embedding/Redis call
    # per graph turn would defeat this suite's "no live services" guarantee
    # (see e.g. test_graph_integration.py's module docstring) just as
    # surely as an unmocked search_docs call would.
    monkeypatch.setattr(graph, "_default_cache_get", lambda ctx, query: None)
    monkeypatch.setattr(graph, "_default_cache_set", lambda ctx, query, answer, citations: None)
