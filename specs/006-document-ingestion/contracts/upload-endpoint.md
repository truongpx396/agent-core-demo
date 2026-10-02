# Contract: Upload Endpoint and Job Stream

**Feature**: [spec.md](../spec.md) | **Data**: [data-model.md §1, §6](../data-model.md) | **Identity**: feature 002 `identity-boundary.md`

**Status**: Retrospective — `app/api/main.py::ingest_upload`, `ingest_stream`, `_read_bounded`; tests in `tests/api/test_api.py`
(`TestIngestUpload`, `TestIngestStream`).

## `POST /ingest/upload`

- **Headers**: `X-Tenant-Id`, `X-Principal-Id` required (a missing one → 422 before the handler). Rate-limited per tenant (feature 001): over the budget → 429.
  **Not** `X-Domain`-aware: ingestion is per tenant, not per domain.
- **Body**: `multipart/form-data` — `files` (≥ 1), `topic` (optional string, unbounded — A4).
- **Response**: `200` and a JSON list of `IngestUploadResult` in request order (data-model §1). The HTTP status is `200` even when *every* file failed; each file's outcome
  is in its own result.
- **Whole-request refusal**: more than `MAX_UPLOAD_FILES_PER_REQUEST` files → `400 {"detail": "<n> files exceeds the <cap>-file limit per upload — split into multiple submissions"}`,
  before any file is touched.
- **Per file, in order**: strip the path from the name → check the suffix (`.pdf`, `.docx` only; case-insensitive) → read in 1 MB chunks, rejecting as soon as the total passes
  the cap → write to storage on a worker thread under `<tenant>/<job id>-<name>` → publish the job → result with `job_id`.
- **Guarantees**: nothing is parsed or embedded in the request; a failing file never stops the others; a stored-but-unqueued file is deleted (best-effort, never raises).
- **Not guaranteed**: that the content is valid (a corrupt PDF is accepted and fails in the worker); that a `done` job produced searchable text (B9).

## `GET /ingest/stream/{job_id}`

- **No identity required**; the id is an unguessable `uuid4().hex` (feature 002 R14).
- **Response**: `text/event-stream`; frames `data: <json>\n\n` with the events of data-model §6; the stream ends after the first `done` or `error`; headers
  `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- **First-event deadline**: if no event is published within `INGEST_FIRST_RESPONSE_DEADLINE_SECONDS` (30 s) the stream yields one `error`
  (`"No response for job '<id>' after 30s — is an ingest-worker running?"`) and ends. The deadline clears on the first real event and never bounds the job.
- **Cleanup**: the job's result stream is deleted when the reader finishes (best-effort); an unread one expires after 300 s. Unlike chat, ingest has exactly one reader per job, so
  eager deletion is safe here.
- **A disconnected reader** does not stop the job.

## Client obligations (the built-in page)

- Show one status line per file; `started` → "processing…"; progress → a percentage bar; `done` → `✓ indexed (<n> chunk[s])`; `error` → `✕ <content>`.
- Mirror the per-request file cap client-side (the server enforces it regardless).

## Invariants a change must preserve

1. The request does no parsing or embedding.
2. Each file succeeds or fails alone.
3. A file in storage always has a job, or is deleted.
4. Every wait on a counterparty has a deadline that never bounds real work.
5. A failure message for the uploader never exposes how the system is wired (**B10**).
