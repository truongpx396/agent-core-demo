# Contract: Chunking and the Stored Record

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §4–§5](../data-model.md) | **Retrieval**: feature 001 (how a record is found, framed as data and cited)

**Status**: Retrospective — `app/ingestion/chunking.py`, `app/ingestion/ingestor.py`, `app/retrieval/qdrant_store.py::build_point`/`upsert`; tests in
`tests/ingestion/test_chunking.py` and `test_ingestor.py`.

## Chunking

`chunk_text(text, parent_chars=1200, child_chars=600, child_overlap=150) -> list[ParentChunk]`

1. `child_overlap >= child_chars` → `ValueError`.
2. Split `text` on `\n\n`, strip each paragraph, drop empties; none left → `[]`.
3. Greedily pack consecutive paragraphs into a parent (joined by `\n\n`) while the parent would not exceed `parent_chars`; a paragraph longer than `parent_chars` closes the
   current parent and is hard-split into `parent_chars` slices.
4. Each parent gets a fresh `uuid4().hex` `parent_id` and its children: if the parent is no longer than `child_chars`, one child; else windows of `child_chars` starting every
   `child_chars − child_overlap` characters, the last possibly short.

Only children are embedded. Parents are never embedded.

## The core: `ingest_text(text, title, ctx, source="text", topic=None, on_progress=None) -> int`

1. `valid_ctx(ctx)` else `IngestRefused("a valid tenant+principal ctx is required to ingest content")` and `agent_ingest_refused_total{reason="no_ctx"}`.
2. `chunk_text`; none → return `0` (no write, no metric, **not an error**).
3. Sparse vectors for *all* children in one `asyncio.to_thread` call; any failure → log and continue dense-only.
4. Dense vectors in batches of `EMBED_BATCH_SIZE` (200), awaiting `on_progress(done, total)` after each.
5. Build one point per child (data-model §4), id `_content_point_id(tenant, source, i, child_text)`, with `build_point`.
6. `qdrant_store.upsert(points)` in batches of at most 300.
7. `agent_ingest_total{source=<source prefix>}` +1; return the number of points.

**Guarantees**: nothing is written without a valid ctx; a repeat of identical input writes identical ids; an embedding failure raises (nothing partial is *promised*, but a re-run
converges). **Not guaranteed**: removal of the records an earlier version of the same source wrote (**B11**).

## Entry-point wrappers

| Function | Adds | Refusals (all `IngestRefused`, counted) |
|----------|------|----------------------------------------|
| `ingest_file(path, ctx, topic)` | reads UTF-8 (errors replaced); `source="file:<name>"` | suffix not `.txt`/`.md` → `bad_file_type` |
| `ingest_url(url, ctx, topic)` | SSRF guard; `GET` with no redirects, 10 s per phase; strips HTML (skips `<script>`/`<style>`); `source="url:<url>"` | unsafe URL → `ssrf_blocked`; transport error or status ≥ 400 → `fetch_failed`; body > 2 MB → `too_large` |
| `scripts/seed.py` | `ensure_collection` first (**recreates the collection — B12**); principal `make-ingest`; `source="sample_docs"` | — |

## Invariants a change must preserve

1. A record is never written without the tenant, the principal and the kind.
2. The record id is derived from content; the namespace constant never changes.
3. Only children are embedded; the parent text always travels with its children.
4. Every write is bounded in size (embedding and upsert batches).
5. Ingesting must not remove content the ingest did not write (**B12**).
