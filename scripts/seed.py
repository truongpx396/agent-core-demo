"""Embed the sample docs via LiteLLM and load them into Qdrant.

Run with: `make ingest`

Just `app/ingestion/ingestor.py::ingest_text` called once per sample doc —
the same general-purpose pipeline (chunking, hybrid dense+sparse
embedding, `qdrant_store.build_point`) any file/URL/pasted-text ingest
goes through (GRAPH_PATTERNS.md pattern 24), not a separate write path.
Each sample doc is short enough to become one parent chunk with one child
— chunking is a no-op here, not a special case.
"""
import asyncio

from app.core.config import DEFAULT_TENANT
from app.core.security import SecurityCtx
from app.ingestion import ingestor
from app.retrieval import qdrant_store
from app.retrieval.embeddings import embed_text
from scripts.sample_docs import DOCS

# Ingestion needs a real SecurityCtx — `ingest_text` refuses without one
# (unowned content is never ingested as tenant-less/public). `make ingest`
# stamps a fixed principal identifying itself as the seeding script.
_INGEST_CTX: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": "make-ingest", "claims": {}}


async def main() -> None:
    try:
        # ensure_collection recreates the collection (destructive), so it's
        # called explicitly once here, never implicitly inside ingest_text
        # (which would wipe prior ingests every call). `dim` comes from a
        # real embed call, never hardcoded.
        await qdrant_store.ensure_collection(dim=len(await embed_text("dimension probe")))

        total_chunks = 0
        for doc in DOCS:
            total_chunks += await ingestor.ingest_text(
                doc["text"],
                title=doc["title"],
                ctx=_INGEST_CTX,
                source="sample_docs",
                topic=doc["topic"],
            )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Failed to ingest sample docs: {exc}\n"
            "Is the stack running? Try `make up` and `make pull-models` first."
        ) from exc

    print(
        f"Ingested {len(DOCS)} sample docs as {total_chunks} chunks into Qdrant "
        f"(tenant={DEFAULT_TENANT!r})."
    )


if __name__ == "__main__":
    asyncio.run(main())
