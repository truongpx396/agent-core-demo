# Implementation Plan: Document Ingestion

**Branch**: `006-document-ingestion` | **Date**: 2026-10-02 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/006-document-ingestion/spec.md`

**Status**: Retrospective — describes the as-built implementation. Every path below exists today.

## Summary

A **fire-and-forget upload pipeline** on a queue of its own. `POST /ingest/upload` (`app/api/main.py`) validates each file
independently (type, size read in 1 MB chunks), writes the bytes to object storage under `<tenant>/<job id>-<file name>`
(`app/ingestion/object_store.py`, MinIO SDK, off the event loop), and publishes a *pointer* job onto the `ingest:requests` Redis
Stream (`app/ingestion/ingest_queue.py`) — a stream and consumer group separate from chat's so a burst of long ingest jobs cannot delay
turns. A store failure after upload deletes the blob. `GET /ingest/stream/{job_id}` relays the job's result stream as SSE.

`app/ingestion/ingest_worker.py` (`python -m app.ingestion.ingest_worker`) runs the same concurrency model as the agent worker: a
semaphore acquired before each task, a thread pool sized to the bound, graceful stop, and an `XAUTOCLAIM` recovery loop that re-runs a
reclaimed job up to `MAX_AUTO_RECLAIM_RETRIES` and then dead-letters it. Per job it downloads (`to_thread`), extracts text by suffix
(`extractors.py`: pypdf, python-docx) and calls `ingestor.ingest_text`.

`ingest_text` is the single write core every entry point funnels through: refuse without a valid ctx → `chunk_text` (parent ~1200 chars on
paragraph boundaries, child 600 with 150 overlap) → one batched sparse (BM25, local, `to_thread`) embedding for the whole document → dense
embeddings in batches of 200 with a progress callback → one `PointStruct` per child with the child text, `parent_id`, `parent_text`,
`title`, `source`, `ingested_by`, `kind="document"`, `tenant` and optional `topic` → `qdrant_store.upsert` in batches of 300. Point ids are
`uuid5(namespace, tenant|source|index|sha256(text))`, so a repeat overwrites itself. `.txt`/`.md` files, a fetched URL (SSRF-guarded) and
`make ingest`'s sample docs use the same core.

The plan records honestly that four reproduced defects (**B9–B12**) and seven smaller gaps sit around a write path whose *safety* properties —
ownership stamped on every record, idempotent identities, bounded sizes, compensation on failure — hold where they are asserted.

## Technical Context

**Language/Version**: Python 3.13

**Primary Dependencies**: `pypdf` and `python-docx` (extraction), `minio` (object storage — chosen over boto3 for size), `qdrant-client`
(1.19.0 installed; `upsert`, `recreate_collection`), `fastembed` (local BM25 sparse), the dense embedding model via the LiteLLM proxy
(`embed` alias), `redis` (Streams, shared with chat), `httpx` (URL fetch), `fastapi` (`UploadFile`, SSE).

**Storage**: Object storage bucket `ingest-uploads` (`MINIO_*`; MinIO locally, an S3-compatible service in production — the production
compose file defines no storage container); Redis — `ingest:requests`, `ingest:requests:dead`, `ingest:results:<job_id>` (TTL 300 s);
Qdrant — the main collection (`COLLECTION`, "docs"), shared with notes and memories, distinguished by `kind`, with a named dense (cosine)
and a named sparse (BM25, IDF modifier) vector. No Postgres tables.

**Testing**: pytest hermetic tier — `tests/ingestion/` (`test_chunking.py`, `test_extractors.py`, `test_ingestor.py`, `test_ingest_queue.py`,
`test_ingest_worker.py`, `test_object_store.py`), and the upload/stream handlers in `tests/api/test_api.py` (`TestIngestUpload`,
`TestIngestStream`) — all against fakes. **Not tested against a real service**: object storage, the ingest stream, the vector store's
upsert, or the end-to-end upload → search path (A1). `tests/live/test_qdrant_real.py` exercises the vector store's search side only.

**Target Platform**: Linux containers; N independently scaled `ingest-worker` replicas.

**Project Type**: Web endpoint + queue worker + library functions + a seeding script.

**Performance Goals**: None asserted. Measured once: a real 9.7 MB PDF became ~5700 chunks; batched dense embedding took ~75 s versus ~13 min
unbatched; sparse embedding ~0.5 s for 5700 chunks.

**Constraints**: per-file cap `MAX_UPLOAD_SIZE_MB = 25`; per-request cap `MAX_UPLOAD_FILES_PER_REQUEST = 5`; `INGEST_WORKER_MAX_CONCURRENCY = 10`;
`INGEST_WORKER_RECLAIM_IDLE_SECONDS = 900`; `INGEST_FIRST_RESPONSE_DEADLINE_SECONDS = 30`; `EMBED_BATCH_SIZE = 200`; upsert batches of 300 (the
store rejects a request over 32 MiB); URL fetch ≤ 2 MB, 10 s per phase, no redirects.

**Scale/Scope**: 2 upload formats, 3 script/library entry points (`.txt`/`.md`, URL, pasted text), 1 worker, 1 queue.

**Unknowns**: none — every value is read from the repository.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design (end of section).*

| # | Principle | Touched? | Verdict | Evidence / gap |
|---|-----------|----------|---------|----------------|
| I | Fail-closed tenant isolation (NN) | **Primary** | **PASS** | `ingest_text` refuses without a valid ctx (counted `reason="no_ctx"`); every point carries `tenant`, `ingested_by` and `kind`; the point id includes the tenant; the storage key begins with the tenant; reads are filtered inside the vector query (features 001/002). Documents are tenant-wide by design. The job stream is not ownership-checked (an unguessable id — feature 002 R14). |
| II | Mandatory human approval (NN) | No | n/a | Ingestion is a person's own action, not an agent tool; no agent can ingest. |
| III | Fixed, typed tools | No | n/a | Not a tool. (`topic` is an unbounded free-form form field — A4 — but is not model-supplied.) |
| IV | Exactly-once side effects (NN) | Yes — at-least-once delivery | **PASS for duplication; B11 is a *staleness* gap** | Point ids are content-addressed, so every retry converges (`_content_point_id`; reclaimed jobs are always re-run). What it does **not** give is *replacement*: an edited document's old chunks are never removed (B11). |
| V | Bounded, observable failure | **Primary** | **PASS with 2 defects (B9, B10) and 2 advisories** | Bounded: file and request caps, bounded reads, batch sizes, first-event deadline, retry cap, bounded dead-letter. Compensated: orphan blob deleted on a publish failure; a failing file never aborts the batch. Observable: `agent_upload_rejected_total`, `agent_upload_failed_total` (alert `IngestUploadFailing`), `agent_ingest_refused_total`, `agent_ingest_total`, `agent_worker_job_reclaimed_total{queue="ingest"}`. **B9**: a zero-chunk result is reported as success. **B10**: raw exception text reaches the uploader. A3 (blobs never deleted), A5 (URL fetch buffers before bounding). |
| VI | Untrusted content is data | Yes | **PASS** | Uploaded text is untrusted; it is framed as data at retrieval (feature 001). The URL entry point uses the shared SSRF guard (https only, every resolved address checked, redirects off). No scrubbing at ingest — not required (scrubbing is for tool output). |
| VII | Test discipline | Yes | **PASS with the known gap (A1)** | Hermetic coverage of chunking, extraction, the core, the queue and the worker. Nothing runs the write path against real object storage, a real stream or the real collection; the idempotence claim rests on the vector store's upsert-by-id semantics, stated in a comment but not integration-tested. |
| VIII | Why-first docs, honest gaps | Yes | **FAIL on B12; PASS otherwise** | `WORKER_CONCURRENCY.md` and the module docstrings carry the reasoning and the measured failures. **B12**: the Makefile help and README describe `make ingest` as an upsert while it recreates the collection. B9–B12 and A1–A6 are not yet in the README Roadmap. |

**Gate result (pre-research)**: no violation of a NON-NEGOTIABLE principle. **B12 is a Principle VIII defect** (a misleading description of a
destructive command) with a data-loss consequence; B9 and B10 are Principle V defects; B11 is a correctness gap. They are *defects*, not
justified exceptions; the plan proceeds because it describes shipped code.

**Post-design re-check (after `research.md`, `data-model.md`, `contracts/`)**: unchanged. Writing the record-identity table in `data-model.md`
§4 is what made B11 visible as a property of *content addressing itself*: it gives convergence on repeats and, by the same mechanism, no way to
tell a superseded chunk from a live one.

## Project Structure

### Documentation (this feature)

```text
specs/006-document-ingestion/
├── plan.md
├── spec.md
├── research.md                    # Phase 0 — decisions + the failures behind each
├── data-model.md                  # Phase 1 — the job, the stored file, the record, identities, state
├── quickstart.md                  # Phase 1 — runnable checks per tier, incl. the B9–B12 reproductions
├── contracts/
│   ├── upload-endpoint.md         # POST /ingest/upload and GET /ingest/stream/{job_id}
│   ├── ingest-job-protocol.md     # the job payload, the worker's event stream, failure and reclaim policy
│   └── record-shape.md            # chunking parameters, the stored record and its identity
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
app/
├── api/main.py                    # ingest_upload, ingest_stream, _read_bounded
├── ingestion/
│   ├── ingestor.py                # ingest_text (the core), ingest_file, ingest_url, SSRF wrapper, _content_point_id
│   ├── chunking.py                # chunk_text: parent/child, paragraph-aware, overlapping
│   ├── extractors.py              # extract_pdf_text, extract_docx_text, ExtractionFailed
│   ├── object_store.py            # upload_bytes, download_bytes, delete_object, ensure_bucket
│   ├── ingest_queue.py            # the stream/group, publish_ingest_request, read_results
│   └── ingest_worker.py           # process_job, run, reclaim loop
├── retrieval/qdrant_store.py      # build_point, upsert (batched), ensure_collection (recreate), delete_by_filter
└── core/url_safety.py             # the shared SSRF guard
scripts/seed.py · scripts/sample_docs.py        # make ingest
postgres-init/                     # (none — no relational tables)
tests/ingestion/ · tests/api/test_api.py (TestIngestUpload, TestIngestStream)
```

**Structure Decision**: One write core (`ingest_text`) with thin front ends per input kind; the upload flow adds only the storage/queue/worker
plumbing. No second pipeline exists to drift.

## Complexity Tracking

> Filled because the Constitution Check found four defects and several gaps. Defects are listed without a justification column: they are
> simply open.

| Violation / advisory | Why Needed | Simpler Alternative Rejected Because |
|----------------------|------------|-------------------------------------|
| **B9 (defect, open)** — an extraction that yields no text is reported as `done` with 0 chunks and shown as "✓ indexed (0 chunks)". Reproduced. | Not needed — blank text is deliberately "not an error" in `ingest_text` (right for a script's empty input), and the worker publishes whatever count it returns. | In `process_job`, treat a 0-chunk result as a *failure* with a specific message ("no extractable text — a scanned PDF? OCR is not supported") and count it; failing test first (extractor returns `""` → terminal `error`). Keep `ingest_text`'s own contract. Its own PR. |
| **B10 (defect, open)** — the worker catch-all publishes `str(exc)` to the uploader. Reproduced. | Not needed — written before the error envelope; the deliberate messages (`ExtractionFailed`, `IngestRefused`) and the unexpected ones share one `except`. | Catch the two expected classes explicitly (their text is written for the uploader) and answer everything else with the generic internal envelope, logging the class; failing test first (a connection error naming an internal host must not appear in the event). Its own PR. |
| **B11 (defect, open)** — an edited re-upload leaves the old passages searchable; no document can be removed. Reproduced. | Not needed — content addressing was added for *idempotence* and nothing tracks "this source's previous generation". | On a successful ingest, delete points for the same `(tenant, source)` whose ids are not in the new set (`delete_by_filter` + an id exclusion), after the upsert so a crash never leaves a gap; failing test first with a recording store; plus a delete-by-source path (feature 002 A3's sibling). Needs a decision on whether `source` is stable across renames. Its own PR. |
| **B12 (defect, open)** — `make ingest` recreates the whole collection and is described as an upsert. | Not needed — `ensure_collection` was written for a schema change (a pre-hybrid collection "must be re-ingested") and the seed script reused it. | Make the seed script create the collection only if it does not exist (`collection_exists`/`create_collection`, which also removes the deprecated call), keep a separate explicit `make ingest-reset` for the destructive case, and correct the Makefile help and README; failing test first (a seed run must not delete a point it did not write). Its own PR; the doc correction can land first. |
| **A1** — no real-backend test of the write path. | Developed hermetically; the real-service tests target retrieval and queues. | An `integration` test using `tests/containers.py` for MinIO (new helper), Redis and Qdrant: upload → worker → search, plus the idempotent re-ingest and B11. Needs a MinIO container helper. |
| **A2** — no document listing or job directory. | The pipeline keeps no per-document state by design. | A small listing by `(tenant, source)` from the vector store (payload scroll) would serve the B11 delete path as well. |
| **A3** — originals are never deleted from object storage. | Nothing consumes them after ingest, but nothing removes them either. | Delete after a successful ingest (or set a bucket lifecycle rule) and on dead-letter; decide whether to keep originals for re-processing. |
| **A4** — `topic` is unbounded/unvalidated and copied onto every chunk. | A convenience field added with the upload form. | A length bound and a character set at the endpoint. |
| **A5** — `ingest_url` buffers the full response before bounding it; timeouts are per phase. | A library function with no production caller. | Stream with a byte counter and a total deadline, or delete the unused entry point. |
| **A6** — a parser's error text is published to the uploader inside `ExtractionFailed`. | The message is useful to the uploader. | Keep the specific *category* ("encrypted", "corrupt") and drop the library text. |
| **A7** — no test for a password-protected PDF. | The extractor tests were written around a real PDF fixture and garbage input. | Generate an encrypted PDF in the test (pypdf can encrypt) and assert `ExtractionFailed("PDF is password-protected")`. |
