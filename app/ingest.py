"""Embed the sample docs via LiteLLM and load them into Qdrant.

Run with: `make ingest`
"""
from qdrant_client.models import PointStruct

from app import qdrant_store
from app.config import DEFAULT_TENANT
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
            # kind + tenant: the same payload shape app/tools.py's add_note
            # writes, and the predicate app/security.py's Policy.lower
            # filters search_docs by — every seeded doc belongs to
            # DEFAULT_TENANT, so a request for any other tenant sees none
            # of them, same as it would see none of another tenant's
            # add_note writes.
            payload={
                "text": doc["text"],
                "topic": doc["topic"],
                "kind": "document",
                "tenant": DEFAULT_TENANT,
            },
        )
        for doc, vec in zip(DOCS, vectors)
    ]
    qdrant_store.upsert(points)
    print(f"Upserted {len(points)} points into Qdrant (tenant={DEFAULT_TENANT!r}).")


if __name__ == "__main__":
    main()
