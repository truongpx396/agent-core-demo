"""Embed the sample docs via LiteLLM and load them into Qdrant.

Run with: `make ingest`

Just `app/ingestor.py::ingest_text` called once per sample doc — the same
general-purpose Ingestor pipeline (chunking, hybrid dense+sparse
embedding, `qdrant_store.build_point`) any file/URL/pasted-text ingest
goes through (GRAPH_PATTERNS.md pattern 24), not a separate write path
that could drift from it. Each sample doc is short enough to become
exactly one parent chunk with one child (see app/chunking.py) — chunking
is a no-op for content this small, not a special case this module has to
work around.
"""
from app import ingestor, qdrant_store
from app.config import DEFAULT_TENANT
from app.embeddings import embed_text
from app.sample_docs import DOCS

# Ingestion needs a real SecurityCtx (app/security.py) — `ingest_text`
# refuses without one (content whose ownership can't be established is
# refused, never ingested as tenant-less/public). `make ingest` is a local
# dev/seed operation with no request to derive one from, so it stamps a
# fixed principal identifying itself as the seeding script, same spirit as
# app/chat.py's local dev ctx.
_INGEST_CTX = {"tenant": DEFAULT_TENANT, "principal": "make-ingest", "claims": {}}


def main() -> None:
    try:
        # ensure_collection recreates the collection (destructive — see its
        # docstring), so it's called explicitly, exactly once, here — never
        # implicitly inside ingest_text, which would wipe prior ingests on
        # every single call. `dim` comes from a real embed call, same as
        # app/semantic_cache.py's index-dimension lookup, never hardcoded.
        qdrant_store.ensure_collection(dim=len(embed_text("dimension probe")))

        total_chunks = 0
        for doc in DOCS:
            total_chunks += ingestor.ingest_text(
                doc["text"],
                title=doc["topic"],
                ctx=_INGEST_CTX,
                source="sample_docs",
                topic=doc["topic"],
            )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Failed to ingest sample docs: {exc}\n"
            "Is the stack running? Try `make up` and `make pull-models` first."
        )

    print(
        f"Ingested {len(DOCS)} sample docs as {total_chunks} chunks into Qdrant "
        f"(tenant={DEFAULT_TENANT!r})."
    )


if __name__ == "__main__":
    main()
