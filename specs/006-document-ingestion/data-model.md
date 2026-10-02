# Data Model: Document Ingestion

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Research**: [research.md](./research.md)

**Status**: Retrospective — read from the code and the queue modules. This feature owns **no relational tables**; its state is an object in
storage, a few Redis streams, and records in the shared vector collection.

## 1. Upload request and per-file result

`POST /ingest/upload` — `multipart/form-data`: `files` (one or more), `topic` (optional text), headers `X-Tenant-Id` / `X-Principal-Id` (feature 002).

`IngestUploadResult` (a list, one per file, in request order): `filename` (the name with any directory part stripped), `job_id` (set on success), `error`
(set instead of `job_id`). Never both.

| Condition | Result | Counter |
|-----------|--------|---------|
| more files than `MAX_UPLOAD_FILES_PER_REQUEST` | the **whole request** → HTTP 400 | `agent_upload_rejected_total{reason="too_many_files"}` |
| suffix not in `{.pdf, .docx}` | that file: `error` naming the supported types | `…{reason="bad_file_type"}` |
| running total over `MAX_UPLOAD_SIZE_MB` | that file: `error` (413 detail text) | `…{reason="too_large"}` |
| storage write or queue publish fails | that file: `error "failed to queue '<name>' for ingestion"`; blob deleted | `agent_upload_failed_total{reason="storage_error"}` |
| otherwise | that file: `job_id` | — |

## 2. Stored file

`object_key = "<tenant>/<job_id>-<file name>"` in bucket `MINIO_BUCKET` (`ingest-uploads`); content type as sent, else `application/octet-stream`.
**Never deleted after a successful or failed ingest** (A3); deleted only by the compensating path.

## 3. Job payload (`ingest:requests`, field `payload`, JSON)

```json
{ "job_id": "<uuid4 hex>", "object_key": "<tenant>/<job_id>-<name>", "filename": "<name>",
  "content_type": "<mime>", "ctx": {"tenant": "…", "principal": "…", "claims": {}}, "topic": "<text or null>" }
```

A reclaimed retry adds `_reclaim_attempts: <n>` (an internal field). The identity is whatever the API stamped from the headers; the worker passes `ctx` to
`ingest_text`, which re-validates it.

## 4. Document record (one vector-store point per child chunk)

| Field | Value |
|-------|-------|
| `id` | `uuid5(NAMESPACE, "<tenant>|<source>|<index>|<sha256(child text)>").hex` — `index` is the child's position in the whole document |
| vector `dense` | the dense embedding of the child text (cosine) |
| vector `sparse` | BM25 term weights (IDF modifier), **optional** (absent if the sparse step failed) |
| payload `text` | the child chunk |
| payload `parent_id` | a fresh `uuid4().hex` per parent **per ingest** (not content-derived) |
| payload `parent_text` | the parent passage (what the model and citations show) |
| payload `title` | the file stem (upload), the URL, or the doc title |
| payload `source` | `upload:<file name>`, `file:<name>`, `url:<url>`, `sample_docs`, or `text` |
| payload `ingested_by` | the ingesting principal |
| payload `kind` | `"document"` |
| payload `tenant` | the tenant |
| payload `topic` | only if given |

The collection also holds notes and memories (`kind` distinguishes them) — feature 001/002. **Identity properties**: the same tenant, source, position and text always yield
the same id (idempotent); a changed chunk yields a *new* id and the old one is never removed (B11); `parent_id` changes on every re-ingest but the records that share it are
all overwritten together, so grouping stays consistent.

## 5. Chunking parameters (`app/ingestion/chunking.py`)

| Parameter | Default | Rule |
|-----------|---------|------|
| `DEFAULT_PARENT_CHARS` | 1200 | paragraphs (`\n\n`-separated, stripped) are packed greedily; a paragraph over the limit is hard-split |
| `DEFAULT_CHILD_CHARS` | 600 | a parent no longer than this is one child |
| `DEFAULT_CHILD_OVERLAP` | 150 | must be less than the child size (else `ValueError`); step = child − overlap; only the last window may be short |

Blank or whitespace-only text → `[]`. Embedding batch `EMBED_BATCH_SIZE = 200`; upsert batch `_MAX_POINTS_PER_UPSERT_BATCH = 300`.

## 6. Job result events (`ingest:results:<job_id>`, TTL 300 s, refreshed on every write)

| Event | Shape | When |
|-------|-------|------|
| `started` | `{type}` | first thing the worker publishes |
| `progress` | `{type, done, total}` | after each dense batch; never for 0 chunks; a failed publish is logged and dropped |
| `done` | `{type, chunks}` | terminal; `chunks` may be **0** (B9) |
| `error` | `{type, content}` | terminal; **no `code`**; `content` is `str(exc)` for unexpected failures (B10), a fixed text for reclaim exhaustion |

## 7. Redis keyspace

| Key | Writer | Reader | Bound |
|-----|--------|--------|-------|
| `ingest:requests` | API | ingest workers (group `ingest-workers`) | none (the queue) |
| `ingest:requests:dead` | worker (reclaim) | operators | ≈ 1000, approximate |
| `ingest:results:<job_id>` | worker | API SSE relay | 300 s; deleted when the reader finishes |

## 8. Entry points → source and kind

| Entry | `source` | Notes |
|-------|----------|-------|
| `POST /ingest/upload` → worker | `upload:<file name>` | PDF/DOCX; the production path |
| `ingestor.ingest_file` | `file:<name>` | `.txt`/`.md`; no production caller |
| `ingestor.ingest_url` | `url:<url>` | SSRF-guarded; no production caller |
| `scripts/seed.py` | `sample_docs` | principal `make-ingest`; **recreates the collection first (B12)** |

## 9. Settings

| Setting | Default | Bounds | Example env |
|---------|---------|--------|-------------|
| `max_upload_size_mb` | 25 | one file | **no** |
| `max_upload_files_per_request` | 5 | one request | **no** |
| `ingest_worker_max_concurrency` | 10 | jobs per worker process (and the thread pool) | **no** |
| `ingest_first_response_deadline_seconds` | 30 | wait for a worker to pick a job up | **no** |
| `ingest_worker_reclaim_idle_seconds` | 900 | idle time before a claimed job is presumed abandoned | **no** |
| `minio_endpoint` / `minio_bucket` / `minio_secure` / keys | `localhost:9000` / `ingest-uploads` / false / dev defaults | object storage | production file only |

The `**no**` entries are feature 004's finding A3 (33 of 61 settings have no example-environment entry).
