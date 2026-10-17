"""Embedding a question and embedding a stored passage are two operations (specs/014 FR-015..FR-017).

The bug this pins: nomic-embed-text-v1.5's card says its input "must include" `search_query: ` for a question and `search_document: `
for a passage, but one function (`embed_text`) served both roles in seven places, so no prefix could ever have been right. The roles are
now separate functions, the old names are gone, and these tests fail if either comes back.
"""
import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.retrieval import embeddings as emb
from app.retrieval import semantic_cache

REPO = Path(__file__).resolve().parents[2]
SCANNED = ["app", "scripts"]
# The evaluation harness drives the app's own embedding client directly, on purpose: it applies a CANDIDATE's declared prefixes so a
# prefix A/B is two candidates over one alias (specs/014 research.md, T039). Nothing else may.
ALLOWED_DIRECT_CLIENT_USERS = {"app/retrieval/embeddings.py"}
ALLOWED_DIRECT_CLIENT_DIRS = ("scripts/model_eval/",)
OLD_NAMES = {"embed_text", "embed_texts"}
RAW_CLIENT_METHODS = {"aembed_query", "aembed_documents", "embed_query", "embed_documents"}


def _python_files():
    for root in SCANNED:
        for path in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path, path.relative_to(REPO).as_posix()


class _FakeClient:
    """Stands in for the OpenAIEmbeddings client and records exactly what text would be sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def aembed_query(self, text):
        self.calls.append(("query", text))
        return [0.1, 0.2, 0.3]

    async def aembed_documents(self, texts, chunk_size=None):
        self.calls.append(("documents", list(texts), chunk_size))
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture
def client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(emb, "embeddings", fake)
    emb.reset_embedding_dim_cache()
    yield fake
    emb.reset_embedding_dim_cache()


# ---- no caller can use neither role ---------------------------------------------------------------------------------------------
def test_nothing_under_app_or_scripts_refers_to_the_removed_single_role_functions():
    offenders = []
    for path, rel in _python_files():
        for node in ast.walk(ast.parse(path.read_text())):
            names = set()
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.name)
            if names & OLD_NAMES:
                offenders.append(f"{rel}:{getattr(node, 'lineno', '?')}")
    assert offenders == [], f"these still use embed_text/embed_texts (pick a role): {offenders}"


def test_only_the_embeddings_module_and_the_harness_call_the_raw_embedding_client():
    """A caller that reached around the role functions could send a question and a passage through the same call again."""
    offenders = []
    for path, rel in _python_files():
        if rel in ALLOWED_DIRECT_CLIENT_USERS or rel.startswith(ALLOWED_DIRECT_CLIENT_DIRS):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {"aembed_query", "aembed_documents"}:
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], f"call app.retrieval.embeddings.embed_query / embed_document(s) instead: {offenders}"


# ---- the prefixes reach the text the model sees ---------------------------------------------------------------------------------
async def test_a_query_is_sent_with_the_query_prefix(client, monkeypatch):
    monkeypatch.setattr(emb, "EMBED_QUERY_PREFIX", "search_query: ")
    await emb.embed_query("refund policy")
    assert client.calls == [("query", "search_query: refund policy")]


async def test_a_passage_is_sent_with_the_document_prefix_and_never_the_query_one(client, monkeypatch):
    monkeypatch.setattr(emb, "EMBED_QUERY_PREFIX", "search_query: ")
    monkeypatch.setattr(emb, "EMBED_DOCUMENT_PREFIX", "search_document: ")
    await emb.embed_document("Refunds take five days.")
    assert client.calls == [("documents", ["search_document: Refunds take five days."], emb.EMBED_BATCH_SIZE)]


async def test_a_batch_of_passages_gets_the_document_prefix_on_every_item_and_the_batch_size_cap(client, monkeypatch):
    monkeypatch.setattr(emb, "EMBED_DOCUMENT_PREFIX", "search_document: ")
    out = await emb.embed_documents(["a", "b"])
    assert len(out) == 2
    assert client.calls == [("documents", ["search_document: a", "search_document: b"], emb.EMBED_BATCH_SIZE)]


async def test_with_the_defaults_the_text_sent_is_byte_identical_to_before_the_change(client):
    """SC-005: no prefix is applied until an operator configures one, so no existing vector changes and nothing needs re-embedding."""
    await emb.embed_query("what are the support hours?")
    await emb.embed_document("Support hours are 9 to 5.")
    await emb.embed_documents(["x", "y"])
    assert client.calls == [
        ("query", "what are the support hours?"),
        ("documents", ["Support hours are 9 to 5."], emb.EMBED_BATCH_SIZE),
        ("documents", ["x", "y"], emb.EMBED_BATCH_SIZE),
    ]


def test_both_prefixes_default_to_empty_and_the_revision_label_too():
    fields = Settings.model_fields
    assert fields["embed_query_prefix"].default == ""
    assert fields["embed_document_prefix"].default == ""
    assert fields["embed_model_revision"].default == ""
    assert fields["embed_identity_cache_seconds"].default == 10.0


# ---- the dimension probe --------------------------------------------------------------------------------------------------------
async def test_the_dimension_is_read_from_a_real_call_once_and_cached(client):
    assert await emb.embedding_dim() == 3
    assert await emb.embedding_dim() == 3
    assert len(client.calls) == 1  # not one probe per collection, per cache, per script


# ---- the semantic cache compares questions with questions ----------------------------------------------------------------------
def test_the_semantic_cache_embeds_with_the_query_role_on_both_sides():
    """It compares a new question with earlier questions, so both must be embedded the same way (FR-017): a document-role vector on
    the write side would be compared with query-role vectors on the read side and never match."""
    tree = ast.parse(Path(semantic_cache.__file__).read_text())
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "embed_query" in called
    assert "embed_document" not in called and "embed_documents" not in called
