"""Embed the sample docs via LiteLLM and load them into Qdrant.

Run with: `make ingest`
"""
from qdrant_client.models import PointStruct

from app import qdrant_store
from app.embeddings import embed_text
from app.sample_docs import DOCS


def main() -> None:
    try:
        vectors = [embed_text(doc["text"]) for doc in DOCS]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Failed to embed documents: {exc}\n"
            "Is the stack running? Try `make up` and `make pull-models` first."
        )

    qdrant_store.ensure_collection(dim=len(vectors[0]))
    points = [
        PointStruct(
            id=doc["id"],
            vector=vec,
            payload={"text": doc["text"], "topic": doc["topic"]},
        )
        for doc, vec in zip(DOCS, vectors)
    ]
    qdrant_store.upsert(points)
    print(f"Upserted {len(points)} points into Qdrant.")


if __name__ == "__main__":
    main()
