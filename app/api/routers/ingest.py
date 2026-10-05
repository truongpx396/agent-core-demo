"""Document upload and its SSE progress.

Fire-and-forget: `POST /ingest/upload` only gets bytes into object storage and a job onto the
ingest queue, which is SEPARATE from the chat-turn queue (see app/ingestion/ingest_queue.py);
parsing and embedding happen in ingest_worker.py. `GET /ingest/stream/{job_id}` relays that job's
progress.
"""
import asyncio
import json
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import get_ctx
from app.api.schemas import IngestUploadResult
from app.core import metrics
from app.core.config import (
    INGEST_FIRST_RESPONSE_DEADLINE_SECONDS,
    MAX_UPLOAD_FILES_PER_REQUEST,
    MAX_UPLOAD_SIZE_MB,
)
from app.core.security import SecurityCtx
from app.ingestion import ingest_queue, object_store
from app.ingestion.extractors import EXTRACTORS_BY_SUFFIX
from app.job_queue import queue

logger = logging.getLogger(__name__)
router = APIRouter()

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1 MB per read() call
_MAX_UPLOAD_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


async def _read_bounded(upload: UploadFile, filename: str) -> bytes:
    """Reads in chunks and rejects as soon as the running total crosses the
    limit — never materializes much more than one chunk past
    `_MAX_UPLOAD_BYTES`, unlike a bare `await upload.read()` which reads the
    entire file first. An unbounded upload is a memory/storage exhaustion
    vector, not just a slow request."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            metrics.agent_upload_rejected_total.labels(reason="too_large").inc()
            raise HTTPException(
                status_code=413,
                detail=f"{filename!r} exceeds the {MAX_UPLOAD_SIZE_MB}MB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/ingest/upload", response_model=list[IngestUploadResult])
async def ingest_upload(
    files: list[UploadFile] = File(...),
    topic: str | None = Form(None),
    ctx: SecurityCtx = Depends(get_ctx),
) -> list[IngestUploadResult]:
    """Upload documents to build this tenant's corpus — PDF/DOCX only today
    (app/ingestion/extractors.py); `.txt`/`.md` have their own path
    (ingestor.py::ingest_file, the CLI/script-driven one).

    Fire-and-forget per file: this handler only gets bytes into MinIO and a
    job onto the queue — parsing/embedding happens in ingest_worker.py, on a
    SEPARATE queue from chat turns (see ingest_queue.py). Each file's
    outcome tracks independently via its own `job_id`
    (`GET /ingest/stream/{job_id}`), so one slow/failing file never blocks
    the others — including a FAILING one: every per-file problem (bad
    extension, too-large, a MinIO/Redis error) is caught and reported back
    as that file's own `IngestUploadResult.error`, rather than raising and
    losing whatever earlier files in the SAME request already succeeded.

    Only "too many files" is rejected synchronously, before touching any
    file — a UX/abuse guard on this one call, distinct from
    INGEST_WORKER_MAX_CONCURRENCY (it limits how many jobs one submission
    creates, not how many a worker runs at once), and genuinely
    request-wide rather than per-file.

    A file that uploads to MinIO but then fails to publish its own job is
    cleaned up (`object_store.delete_object`) rather than left as an
    orphaned blob nothing will ever ingest or remove.
    """
    if len(files) > MAX_UPLOAD_FILES_PER_REQUEST:
        metrics.agent_upload_rejected_total.labels(reason="too_many_files").inc()
        raise HTTPException(
            status_code=400,
            detail=f"{len(files)} files exceeds the {MAX_UPLOAD_FILES_PER_REQUEST}-file "
            "limit per upload — split into multiple submissions",
        )
    client = queue.get_client()  # ingest_queue reuses this same Redis client — see its module docstring
    results = []
    for upload in files:
        filename = Path(upload.filename or "").name  # strip any path component a client might send
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTRACTORS_BY_SUFFIX:
            metrics.agent_upload_rejected_total.labels(reason="bad_file_type").inc()
            results.append(
                IngestUploadResult(
                    filename=filename,
                    job_id=None,
                    error=f"unsupported file type {suffix!r} — only "
                    f"{sorted(EXTRACTORS_BY_SUFFIX)} are supported",
                )
            )
            continue

        try:
            data = await _read_bounded(upload, filename)
        except HTTPException as exc:
            # _read_bounded's own too-large rejection (already metriced
            # there) — a per-file problem, not a reason to abort the rest
            # of this batch.
            results.append(IngestUploadResult(filename=filename, job_id=None, error=str(exc.detail)))
            continue

        job_id = uuid.uuid4().hex
        object_key = f"{ctx['tenant']}/{job_id}-{filename}"
        try:
            # Blocking MinIO I/O off the event loop — this IS the shared
            # SSE-serving process, so a large upload must not stall other requests.
            await asyncio.to_thread(
                object_store.upload_bytes,
                object_key,
                data,
                upload.content_type or "application/octet-stream",
            )
            await ingest_queue.publish_ingest_request(
                client,
                job_id=job_id,
                object_key=object_key,
                filename=filename,
                content_type=upload.content_type or "application/octet-stream",
                ctx=ctx,
                topic=topic,
            )
        except Exception as exc:  # noqa: BLE001 - one file's storage/queue failure must not sink the whole batch
            # Best-effort cleanup: harmless even if upload_bytes itself is
            # what failed (deleting an object that was never created is a
            # no-op under MinIO/S3 delete semantics).
            await asyncio.to_thread(object_store.delete_object, object_key)
            metrics.agent_upload_failed_total.labels(reason="storage_error").inc()
            logger.warning(
                # "filename" is a reserved stdlib LogRecord attribute (the
                # source file of THIS log call) — same collision
                # app/domains/notify.py's own docstring already notes for
                # "message"; "upload_filename" instead.
                "ingest_upload_failed",
                extra={"upload_filename": filename, "error_class": type(exc).__name__},
            )
            results.append(
                IngestUploadResult(
                    filename=filename, job_id=None, error=f"failed to queue {filename!r} for ingestion"
                )
            )
            continue

        results.append(IngestUploadResult(filename=filename, job_id=job_id, error=None))
    return results


@router.get("/ingest/stream/{job_id}")
async def ingest_stream(job_id: str) -> StreamingResponse:
    """SSE progress for one upload's job — `{"type": "started"}`, zero or
    more `{"type": "progress", "done": N, "total": M}` while
    ingest_worker.py's embedding loop runs, then one terminal event:
    `{"type": "done", "chunks": N}` or `{"type": "error", ...}`.
    Deliberately NOT ownership-checked against sessions.py-style records
    (this pipeline keeps no job directory) — `job_id` is a `uuid4().hex`,
    unguessable in practice, same posture as the chat results streams.
    """
    client = ingest_queue.get_client()

    async def generate():
        try:
            async for event in ingest_queue.read_results(
                client, job_id, first_event_deadline_seconds=INGEST_FIRST_RESPONSE_DEADLINE_SECONDS
            ):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await ingest_queue.delete_results_stream(client, job_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
