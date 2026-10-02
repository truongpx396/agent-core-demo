# Research: Document Ingestion

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-10-02

**Status**: Retrospective — decisions reconstructed from the code, its comments, `WORKER_CONCURRENCY.md` and `GRAPH_PATTERNS.md`
patterns 13, 20 and 24. Each entry names its evidence. **No `NEEDS CLARIFICATION` remains.** R16–R20 (Part D) are *findings* from
verifying the as-built system, not decisions anyone made.

Format: **Decision** · **Rationale** · **Alternatives considered** · **Evidence**. *Alternatives are those the code or its docs name or
argue against; where none is recorded the entry says so rather than inventing one.*

---

## Part A — The upload pipeline

### R1. Upload is fire-and-forget on a queue of its own

- **Decision**: `POST /ingest/upload` gets bytes into object storage and a job onto `ingest:requests`, then returns; parsing and embedding
  happen in `ingest_worker.py`. The ingest stream and consumer group are separate from chat's.
- **Rationale**: Chat turns are short and latency-sensitive; parsing a large PDF can run for minutes. A shared consumer group round-robins
  without job-type priority, so a burst of ingest jobs would delay chat. Separate queues scale independently.
- **Alternatives considered**: ingesting inside the request (rejected: ties the HTTP tier to the slowest file); a shared queue (rejected, above).
- **Evidence**: `app/ingestion/ingest_queue.py` module docstring; `app/api/main.py::ingest_upload` docstring.

### R2. The job carries a pointer, never the bytes

- **Decision**: The payload holds `object_key`, `filename`, `content_type`, `ctx` and `topic`; the file lives in object storage.
- **Rationale**: Keeps stream entries small regardless of document size, and lets any worker replica fetch the file.
- **Evidence**: `ingest_queue.py::publish_ingest_request` docstring.

### R3. Object storage through the MinIO SDK, not boto3

- **Decision**: A thin wrapper over the `minio` package (`upload_bytes`, `download_bytes`, `delete_object`, lazy `ensure_bucket`), against MinIO
  locally and an S3-compatible service in production.
- **Rationale**: Smaller and purpose-built for put/get/bucket-exists against one self-hosted target; boto3's AWS SDK tree is much larger. A real
  blob store (bucket/key model, multi-instance-ready) rather than a local volume matches the app's self-hosted-backing-store posture.
- **Alternatives considered**: boto3 (rejected, size); a local-disk volume (rejected: not multi-instance-ready).
- **Evidence**: `app/ingestion/object_store.py` module docstring; `.env.prod.example` (the production endpoint).

### R4. Bounded reads, per-file independence, a request-wide count cap

- **Decision**: `_read_bounded` reads in 1 MB chunks and rejects the instant the running total passes `MAX_UPLOAD_SIZE_MB` (25), so it never holds
  much more than one chunk past the cap. A bad extension or an oversized file is reported against that file only. Only "too many files" is rejected
  synchronously, before any file is touched.
- **Rationale**: An unbounded upload is a memory and storage exhaustion vector, not just a slow request; a bare `await upload.read()` reads the whole
  file first. One failing file must not lose the earlier files in the same request. The count cap is a UX/abuse guard on one call and is *not* worker
  throughput (`INGEST_WORKER_MAX_CONCURRENCY` is a different knob).
- **Evidence**: `app/api/main.py::_read_bounded`, `ingest_upload`; `WORKER_CONCURRENCY.md` ("A related-but-different knob");
  `tests/api/test_api.py::TestIngestUpload`.

### R5. Compensate, and count rejection and failure separately

- **Decision**: A file stored but not queued is deleted (`object_store.delete_object`, best-effort, never raises); the failure is counted in
  `agent_upload_failed_total{reason="storage_error"}`, distinct from `agent_upload_rejected_total` (pre-write validation). The former has the alert
  `IngestUploadFailing` (`increase(...[15m]) > 0` for 15 m).
- **Rationale**: Constitution Principle V — a multi-step operation that fails part-way compensates for what it already did; a path that can leave a
  person unaware (an accepted upload that silently never ingests) needs an alert, not just a counter.
- **Evidence**: `ingest_upload`'s `except` block; `app/core/metrics.py`; `observability/prometheus/alerts.yml`;
  `test_api.py::TestIngestUpload::test_a_publish_failure_deletes_the_already_uploaded_object_and_reports_the_file_failed`.

---

## Part B — The write core

### R6. Small-to-big: embed children, show parents

- **Decision**: `chunk_text` packs `\n\n`-separated paragraphs into ~1200-char parents (a longer paragraph hard-split), then slides 600-char child
  windows with 150 overlap over each parent. Only children are embedded and matched; the parent text rides in the payload and is what the model and
  citations show.
- **Rationale**: One chunk size fights two jobs — retrieval wants small and precise, answer quality wants surrounding context. Overlap ensures a
  fact straddling a cut is captured whole by at least one child. The child size was *widened* later while the parent stayed put (2026-09-14); the
  parent is what is injected per citation, and up to five can be cited in one turn — doubling it risks the ~2300–2800-token range where the small local
  model was measured dropping the citation-format instruction (pattern 13).
- **Alternatives considered**: a single chunk size (rejected, above); a larger parent (rejected on the measurement).
- **Evidence**: `app/ingestion/chunking.py`; pattern 24; `tests/ingestion/test_chunking.py`.

### R7. Record ids are derived from content, not drawn at random

- **Decision**: `_content_point_id` = `uuid5(fixed namespace, "<tenant>|<source>|<index>|<sha256(text)>")`.
- **Rationale**: A retried or reclaimed job, or a double-submitted upload, recomputes the same ids at the same positions, so the vector store's
  upsert-by-id overwrites instead of duplicating. `index` and the text hash are both included: position alone would already be stable across an
  identical re-ingest, but folding the content in means a chunking change that shifts what lands at a position gets a fresh id instead of silently
  overwriting a mismatched older chunk. The tenant is included because the vector store keeps tenants apart only by payload filter, so the id space must
  keep them apart on its own. The namespace is a constant that must never change or every existing id shifts.
- **Alternatives considered**: `uuid4()` (the original; a retry then duplicated every chunk); a dedup table (rejected: content addressing needs none).
- **Evidence**: `ingestor.py::_content_point_id`; `tests/ingestion/test_ingestor.py::TestIngestText` (identical content → same ids; different content,
  tenant or source → different ids). **What it cannot do**: tell a superseded chunk from a live one — B11 (R18).

### R8. Batch the slow parts, and bound each request

- **Decision**: Sparse (BM25, local ONNX) embeddings are computed once for the whole document via `asyncio.to_thread`; dense embeddings go in batches
  of 200 with a progress callback after each; `qdrant_store.upsert` writes at most 300 points per call.
- **Rationale**: A real 9.7 MB PDF (~5700 chunks) took ~13 min with one embedding call per chunk and ~75 s batched; batching also gives natural progress
  checkpoints (~28 for that document). The same PDF serialized to ~94 MB in one upsert and the store rejected it (limit 32 MiB) *after* the embedding work
  was spent; 300 per batch keeps a request well under the limit with ~5× headroom. A sparse failure degrades to dense-only rather than failing the document.
- **Evidence**: `ingestor.py::ingest_text`, `_sparse_vectors_or_none`; `qdrant_store.py::upsert`; `app/retrieval/embeddings.py::EMBED_BATCH_SIZE`;
  `tests/ingestion/test_ingestor.py` (`test_sparse_embedding_failure_degrades_to_dense_only`, `test_embeds_all_chunks_in_one_batched_call_each`).

### R9. The worker mirrors the agent worker's concurrency model

- **Decision**: Semaphore acquired before the task; the default executor replaced by a `ThreadPoolExecutor(max_workers=INGEST_WORKER_MAX_CONCURRENCY)`;
  download and extraction through `to_thread`.
- **Rationale**: Download and extraction are synchronous and CPU-bound; without `to_thread` several jobs "running concurrently" would overlap nothing. The
  executor is sized explicitly because the default (`min(32, cpu+4)`) silently caps concurrency below a deliberately raised setting. The GIL still serializes
  CPU-bound parsing: for parse-dominated workloads, more worker *processes* add real parallelism; `to_thread` overlaps the I/O portions only.
- **Evidence**: `ingest_worker.py::run`; `WORKER_CONCURRENCY.md` ("Model 2"); `tests/ingestion/test_ingest_worker.py::TestConcurrentDispatch`,
  `TestRunLoop::test_sizes_the_default_executor_to_max_concurrency`.

### R10. A reclaimed ingest job is always re-run (up to the cap)

- **Decision**: `_handle_reclaimed_job` republishes (capped by `MAX_AUTO_RECLAIM_RETRIES`), then tells the uploader to upload again and dead-letters.
- **Rationale**: Unlike a chat turn, the only side effect is the vector upsert, which is idempotent by construction (R7), so there is no equivalent of a
  mutating tool call to guard. The idle threshold (900 s) is far above the agent worker's because parsing can take minutes.
- **Evidence**: `ingest_worker.py::_handle_reclaimed_job`; `TestHandleReclaimedJob`; feature 003's `queue-job-protocol.md`.

### R11. Blank text is "not an error" at the library level

- **Decision**: `chunk_text` returns `[]` for blank text and `ingest_text` returns 0 without raising or counting.
- **Rationale**: Correct for a script's empty input. **Consequence**: the worker has no signal to distinguish "empty document" from "success", so it publishes
  `done {chunks: 0}` — B9 (R16).
- **Evidence**: `ingestor.py::ingest_text`; `tests/ingestion/test_ingestor.py::TestIngestText::test_blank_text_writes_nothing_and_is_not_an_error`.

### R12. Extraction is best-effort, in-memory and format-specific

- **Decision**: `extract_pdf_text` (pypdf; pages joined with blank lines; encrypted → `ExtractionFailed`) and `extract_docx_text` (paragraph text only) work on bytes,
  never a path; `EXTRACTORS_BY_SUFFIX` maps `.pdf` and `.docx`. `ExtractionFailed` is "an expected, caller-facing outcome, not a bug".
- **Rationale**: The document lives in object storage, not on the worker's disk. "Good enough for retrievable, citable chunks", not a faithful converter: no OCR,
  tables, headers, footers or embedded objects.
- **Evidence**: `app/ingestion/extractors.py`; `tests/ingestion/test_extractors.py`.

---

## Part C — Other entry points and operation

### R13. The URL entry point is SSRF-guarded and bounded

- **Decision**: `ingest_url` requires https, resolves **every** A/AAAA record and rejects if any is private, loopback or reserved, disables redirects, and refuses a
  failing status, a transport error or a body over 2 MB — each counted by reason.
- **Rationale**: A validated URL that redirects to an unvalidated one would reintroduce the exact surface the guard closes. A validate-then-fetch gap (DNS rebinding)
  is disclosed in the shared guard.
- **Evidence**: `ingestor.py::_assert_safe_url`, `ingest_url`; `app/core/url_safety.py`; `tests/ingestion/test_ingestor.py::TestAssertSafeUrl`, `TestIngestUrl`.
  **Caveats**: no production caller; the size check runs after the whole body is buffered (A5).

### R14. One pipeline for seeding, and the collection is recreated once, explicitly

- **Decision**: `scripts/seed.py` (`make ingest`) calls `ensure_collection` once, then `ingest_text` per sample doc under the identity `make-ingest`.
- **Rationale**: One ingest pipeline means one place it can drift. `ensure_collection` *recreates* the collection because a collection built before hybrid search
  (a single unnamed dense vector) is schema-incompatible and "must be re-ingested"; it is called explicitly, never inside `ingest_text`, because inside it would wipe prior
  ingests on every call. **Consequence**: the command also wipes every other tenant's documents, notes and memories — B12 (R19).
- **Evidence**: `scripts/seed.py`; `app/retrieval/qdrant_store.py::ensure_collection`.

### R15. The job stream trusts an unguessable id

- **Decision**: `GET /ingest/stream/{job_id}` takes no identity and does no ownership check; `job_id` is a `uuid4().hex`. The stream is deleted when the reader finishes; an
  unread one expires after 300 s. The first-event deadline (30 s) bounds only the wait for a worker to pick the job up.
- **Rationale**: The pipeline keeps no job directory to check against; the same posture as chat results streams (feature 002 R14). An ingest job's `started` event arrives
  almost immediately after pickup, well before extraction, which can legitimately take minutes.
- **Evidence**: `ingest_stream` docstring; `ingest_queue.py::read_results`.

---

## Part D — Findings (not decisions)

### R16. FINDING B9 — "indexed (0 chunks)"

- **Observation**: `process_job` publishes `done` with whatever `ingest_text` returns; for blank text that is 0; the page renders `✓ indexed (0 chunks)`.
- **Reproduction** (scratch harness): an extractor returning `""` → the events were `[started, done{chunks:0}]`.
- **Consequence**: a scanned (image-only) PDF — no text layer, no OCR — is reported as a success and is not searchable.
- **Options**: treat zero chunks as a specific failure at the worker; add OCR (out of scope). Left open and disclosed.

### R17. FINDING B10 — raw exception text to the uploader

- **Observation**: `process_job`'s `except Exception` publishes `{"type": "error", "content": str(exc)}`; the page shows `✕ <content>`. No error code.
- **Reproduction**: `object_store.download_bytes` raising a connection error naming `minio.internal.example:9000` → the published event carried the text verbatim.
- **Consequence**: internal host names, ports and driver messages reach whoever uploaded. Unlike the chat catch-all, this one also carries *deliberate* uploader-facing
  messages, so it needs a per-class decision.

### R18. FINDING B11 — no replacement, no removal

- **Observation**: nothing on the ingest path deletes a record; the only deletion function (`qdrant_store.delete_by_filter`) is called by the memory-erasure helper (feature 002 A3).
- **Reproduction**: ingest "…within 90 days…" then the corrected "…within 30 days…" under one `source` against a fake store with upsert-by-id semantics → both remain.
- **Consequence**: a retrieval can return, and the model cite, a policy the organization has corrected. Uploaded originals also persist (A3).

### R19. FINDING B12 — the seeding command is destructive and mislabelled

- **Observation**: `Makefile` `ingest: ## Embed sample docs and upsert them into Qdrant`; the README table says "Embed sample docs → Qdrant"; `seed.py` calls `ensure_collection`,
  which calls `recreate_collection`. Confirmed in the installed qdrant-client (1.19.0): `AsyncQdrantRemote.recreate_collection` is `delete_collection` then `create_collection`. The
  method is deprecated in that release.
- **Consequence**: running `make ingest` against a stack with real uploads, notes or memories erases them for every tenant. `scripts/index_skills.py` has the same shape but targets its
  own, rebuildable collection (feature 007).

### R20. FINDINGS A1–A7

- **A1**: grep of `tests/live`, `tests/integration` and `tests/deepeval` finds no reference to `ingest_text`, `ingest_worker`, `ingest_upload` or `/ingest/`.
- **A3**: `object_store.delete_object` has exactly one caller, the compensating path in `ingest_upload`.
- **A4/A5/A6**: `topic` is `str | None = Form(None)` with no validation; `ingest_url` checks `len(response.content)` after the full body is read, with `httpx` timeouts per phase;
  `ExtractionFailed("could not parse PDF: {exc}")` embeds the parser's message. **A7**: `tests/ingestion/test_extractors.py` has no encrypted-PDF case.

---

## Deferred / unbuilt (carried to `tasks.md`)

| Id | Item | Why deferred |
|----|------|--------------|
| B9 | Report a zero-chunk result as a specific failure; count it | Test first; small |
| B10 | Generic envelope for unexpected worker failures; keep the deliberate messages | Needs the per-class split; test first |
| B11 | Replace a source's earlier version on re-ingest; a delete-by-source path; a document listing | Needs a decision on source stability; test first |
| B12 | Seed without recreating; a separate explicit reset; correct the help text | Doc fix can land first; test first for the code |
| A1 | An integration test of upload → worker → search | Needs a MinIO container helper; Docker |
| A2 | A document listing | Pairs with B11's delete |
| A3 | Delete originals after ingest or set a lifecycle rule; on dead-letter | Retention decision |
| A4 | Bound and validate `topic` | Small |
| A5 | Stream-bound `ingest_url`, or remove it | No production caller |
| A6 | Drop the library text from `ExtractionFailed` | Wording |
| A7 | An encrypted-PDF test | Small |
| — | OCR, tables and layout extraction, other formats, per-user ownership, a document library UI | Out of scope |
