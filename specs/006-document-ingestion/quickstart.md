# Quickstart: Validate Document Ingestion

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Contracts**: [contracts/](./contracts/)

A validation guide: what to run, what you should see, which requirement it proves. Cheapest tier first.
**Activate the venv**: `source .venv/bin/activate`.

> **Read this first.** A green Tier 1 proves chunking, extraction, the write core's logic, the queue and worker against fakes, and the endpoint's
> validation. It does **not** prove the real object store, the real stream or the real vector collection behave as the fakes assume (no integration
> test exists — A1) — and it did **not** catch B9–B12, which Scenarios B9–B12 below reproduce. Those four scenarios are expected to show the
> defect *as the system stands*.

---

## Tier 1 — Hermetic (no services, ~2 s)

```bash
pytest tests/ingestion --ignore=tests/ingestion/test_web_crawler.py \
       tests/api/test_api.py::TestIngestUpload tests/api/test_api.py::TestIngestStream -q
```

**Expected** (observed 2026-10-02): `90 passed`.

| Requirement | Evidence |
|-------------|----------|
| FR-001/FR-002/FR-003 per-file validation, request cap, bounded read | `tests/api/test_api.py::TestIngestUpload` (`…unsupported_extension_is_reported_per_file…`, `…bad_file_alongside_a_good_one…`, `…too_many_files…rejected_before_any_upload`, `…over_the_size_cap…`, `…within_the_size_cap…`) |
| FR-004/FR-005 key shape, pointer job, one result per file, orphan deleted on a publish failure | `TestIngestUpload` (`…uploads_to_minio_and_publishes_a_job_per_file`, `…a_publish_failure_deletes_the_already_uploaded_object…`, `…a_path_component_in_the_filename_is_stripped`), `tests/ingestion/test_ingest_queue.py::TestPublishIngestRequest`, `test_object_store.py` |
| FR-007 download → extract → ingest → events; progress ticks in order | `tests/ingestion/test_ingest_worker.py::TestProcessJob` |
| FR-008 a corrupt file fails with a message | `test_extractors.py` (garbage and empty bytes → `ExtractionFailed`; **no test for a password-protected PDF — A7**), `TestProcessJob::test_an_extraction_failure_publishes_an_error_and_still_acks` |
| FR-011 a failure still acks | `TestProcessJob` (`…download_failure…`, `…extraction_failure…`, `…ingest_refusal…`) |
| FR-012 first-event deadline | `tests/ingestion/test_ingest_queue.py::TestPublishResultAndReadResults` |
| FR-013 refusal without a ctx | `tests/ingestion/test_ingestor.py::TestIngestText::test_refuses_without_ctx` |
| FR-014/FR-015 payload fields; tenant in the id | `TestIngestText` (`…one_point_per_child_chunk_with_parent_fields`, `…different_tenants_never_collide…`) |
| FR-016/SC-003 identical content → identical ids; different content/source → different ids | `TestIngestText` |
| FR-017 reclaim → republish, then dead-letter | `test_ingest_worker.py::TestHandleReclaimedJob` |
| FR-019 chunking rules | `tests/ingestion/test_chunking.py` |
| FR-020 batching and the sparse fallback | `TestIngestText` (`…sparse_embedding_failure_degrades_to_dense_only`, `…embeds_all_chunks_in_one_batched_call_each`) |
| FR-021/FR-022 file suffixes; the URL guard and bounds | `TestIngestFile`, `TestAssertSafeUrl`, `TestIngestUrl` |
| FR-024 concurrency bounded and overlapping; the executor sized | `test_ingest_worker.py::TestConcurrentDispatch`, `TestRunLoop` |

**Not covered here**: FR-009/SC-006 (B9), FR-010/SC-007 (B10), FR-018/FR-026/SC-005 (B11), FR-023/SC-009 (B12), FR-027 (A1).

---

## Tier 2 — Real services (Docker)

There is **no** integration test for this feature. `make test-integration` does not touch object storage or the ingest stream, and the live tier's
real-collection test (`tests/live/test_qdrant_real.py`) exercises search, not ingestion. Principle VII known gap — see tasks.

---

## Scenario B9 — Reproduce: a document with no text is reported as indexed (hermetic; expected: it reproduces)

In a scratch Python session (do not commit), with no services:

1. Patch `app.ingestion.ingest_worker.object_store.download_bytes` → returns `b"%PDF-fake"`; patch `EXTRACTORS_BY_SUFFIX[".pdf"]` → a function returning `""`.
2. Build a job entry for `scan.pdf` (a valid `ctx`) and call `await process_job(FakeRedis(), "1-0", fields)` (`FakeRedis` from `tests/job_queue/test_queue.py`).
3. Read `ingest:results:<job_id>`.

**Observed 2026-10-02**: `[{'type': 'started'}, {'type': 'done', 'chunks': 0}]` — the page would show "✓ indexed (0 chunks)". **Fixed when**: the terminal event is an
`error` saying no extractable text was found. This is the failing test the B9 fix starts with.

## Scenario B10 — Reproduce: an internal error reaches the uploader (hermetic; expected: it reproduces)

1. As B9, but patch `download_bytes` to raise `RuntimeError("HTTPConnectionPool(host='minio.internal.example', port=9000): Max retries exceeded")`.

**Observed 2026-10-02**: `[{'type': 'started'}, {'type': 'error', 'content': "HTTPConnectionPool(host='minio.internal.example', port=9000): Max retries exceeded"}]`.
**Fixed when**: the event is generic and carries no host name, while an `ExtractionFailed("PDF is password-protected")` still reaches the uploader verbatim.

## Scenario B11 — Reproduce: a corrected document leaves the old wording searchable (hermetic; expected: it reproduces)

1. Replace `ingestor.qdrant_store.upsert` with a function that stores `point.id → point.payload["text"]` in a dict (upsert-by-id, like the real store); stub the dense embedding and
   the sparse step.
2. `ingest_text("Refunds are accepted within 90 days of purchase.", "policy", ctx, source="upload:policy.pdf")`, then
   `ingest_text("Refunds are accepted within 30 days of purchase.", "policy", ctx, source="upload:policy.pdf")`.

**Observed 2026-10-02**: the dict holds **both** sentences. **Fixed when**: only the "30 days" record remains for that source.

## Scenario B12 — Inspect: the seeding command recreates the collection (read-only; expected: it recreates)

```bash
grep -n "recreate_collection" app/retrieval/qdrant_store.py
python - <<'PY'
import inspect
from qdrant_client.async_qdrant_remote import AsyncQdrantRemote
print([l.strip() for l in inspect.getsource(AsyncQdrantRemote.recreate_collection).splitlines() if "collection(" in l])
PY
grep -n "^ingest:" Makefile
```

**Observed 2026-10-02**: `ensure_collection` calls `recreate_collection`; the installed client's method calls `delete_collection` then `create_collection`; the Makefile help says
"Embed sample docs and upsert them into Qdrant". **Do not run `make ingest` against a stack whose uploads, notes or memories you want to keep.**

## Tier 3 — Full local stack, manual walk-through

**Prerequisites**: `make up`, `make pull-models`, `make serve`, `make ingest-worker`. (This walk-through uploads a file; use a scratch tenant.) Helper (a function — unquoted variables
are not word-split in zsh):

```bash
up() { curl -s -X POST localhost:8000/ingest/upload -H 'X-Tenant-Id: scratch' -H 'X-Principal-Id: alice' -F "files=@$1" ${2:+-F "topic=$2"}; }
```

| # | Do | Expected | Proves |
|---|----|----------|--------|
| 1 | `up some.pdf` | `[{"filename":"some.pdf","job_id":"…","error":null}]` immediately | FR-004 |
| 2 | `curl -N localhost:8000/ingest/stream/<job_id>` | `started`, a few `progress`, `done` with a chunk count | FR-007 |
| 3 | ask the assistant (tenant `scratch`) a question the PDF answers | an answer with a `[1]` citation naming the file | FR-014 |
| 4 | the same question as tenant `ecorp` | no content from the PDF | SC-002 |
| 5 | `up notes.txt` | `unsupported file type '.txt' — only ['.docx', '.pdf'] are supported` | FR-001 |
| 6 | upload a file over 25 MB | a per-file 413-style error; no job | FR-003 |
| 7 | upload 6 files in one request | HTTP 400 and no job | FR-002 |
| 8 | stop the ingest worker, `up some.pdf`, open the stream | after ~30 s an `error` "is an ingest-worker running?" | FR-012, SC-008 |
| 9 | upload the same PDF twice | both complete; the collection's point count does not grow the second time | FR-016, SC-003 |
| 10 | upload a scanned (image-only) PDF | **today: `✓ indexed (0 chunks)` (B9)** | FR-009 |
| 11 | `kill -9` the worker mid-job; wait `INGEST_WORKER_RECLAIM_IDLE_SECONDS` (900 s; lower it in `.env`) | the job is re-run and completes without duplicates | FR-017, SC-004 |

### Looking at the stored file

The MinIO console is at `http://localhost:9001` (`minioadmin` / `minioadmin` locally): bucket `ingest-uploads`, key `scratch/<job_id>-some.pdf`. It is still there after the job finishes (A3).

## Checking the alerts

`IngestUploadFailing` is a rule-file entry (`observability/prometheus/alerts.yml`): stop MinIO and upload — `agent_upload_failed_total{reason="storage_error"}` increments and, held for 15 minutes, the alert fires.
There is no alert for a dead-lettered ingest job (feature 004 A4).

## Troubleshooting

- *The stream reports "is an ingest-worker running?"*: start `make ingest-worker`; it needs Redis and MinIO.
- *`done` with 0 chunks*: the file has no extractable text (B9).
- *Scenario B9–B11 do not reproduce*: confirm you are not on a branch that already fixed them.
- *Do not* run `make ingest`, `make clean`, `clear-*` or `restart-all` while validating.
