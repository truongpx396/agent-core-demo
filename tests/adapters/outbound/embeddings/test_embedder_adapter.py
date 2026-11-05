"""`HybridEmbedder` (`app/adapters/outbound/embeddings/embeddings.py`), the `Embedder` port (`app/ports/outbound/retrieval.py`) the ingest pipeline reaches."""
from app.adapters.outbound.embeddings import embeddings


async def test_the_embedder_returns_what_the_embedding_functions_return(monkeypatch):
    async def dense(texts):
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr(embeddings, "embed_documents", dense)
    monkeypatch.setattr(embeddings, "embed_sparse_batch", lambda texts: [([1], [float(len(t))]) for t in texts])
    embedder = embeddings.HybridEmbedder()

    assert await embedder.embed_documents(["a", "bb"]) == [[1.0], [2.0]]
    assert embedder.embed_sparse_batch(["a", "bb"]) == [([1], [1.0]), ([1], [2.0])]
