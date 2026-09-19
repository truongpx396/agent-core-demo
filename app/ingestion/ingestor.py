"""General-purpose Ingestor: files, URLs, or raw text → retrievable, citable
Qdrant points (GRAPH_PATTERNS.md pattern 24). `scripts/seed.py`
(`make ingest`) is just this module's `ingest_text` called once per sample
doc — one ingest pipeline, one place it can drift.

Every item is chunked (`app/ingestion/chunking.py`'s parent-child, sliding-
window strategy) and embedded the way `hybrid_search` expects (dense+sparse,
via `app/retrieval/embeddings.py`, through `qdrant_store.build_point` — same
shape `add_note`/`remember` use), so ingested content shares the same
retrieval path as everything else.

Every item is stamped with `tenant`/`principal` from a `SecurityCtx` and
refused without one — ownerless content is never ingested as tenant-less/
public (mirrors `app/core/security.py`'s fail-closed discipline).
"""
# socket is unused directly below but kept imported: tests monkeypatch
# ingestor.socket.getaddrinfo, and since `socket` is a shared module in
# sys.modules, that mutation is visible to url_safety.py too (where the
# actual lookup now runs).
import html.parser
import logging
import socket  # noqa: F401
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import httpx

from app.core import metrics
from app.core.security import SecurityCtx, valid_ctx
from app.core.url_safety import UnsafeURLError
from app.core.url_safety import assert_safe_url as _assert_safe_url_impl
from app.ingestion.chunking import chunk_text
from app.retrieval import qdrant_store
from app.retrieval.embeddings import EMBED_BATCH_SIZE, embed_sparse_batch, embed_texts

logger = logging.getLogger(__name__)

_ALLOWED_FILE_SUFFIXES = {".txt", ".md"}
_MAX_URL_BYTES = 2_000_000  # 2 MB — bounded fetch size, part of the SSRF/DoS guard
_URL_TIMEOUT_SECONDS = 10


class IngestRefused(Exception):
    """A refused ingest (bad ctx, disallowed file type, SSRF-blocked URL,
    fetch too large, ...) — expected and caller-facing, not a bug. Every
    raise site also records `agent_ingest_refused_total{reason=...}`
    (app/core/metrics.py)."""


def _sparse_vectors_or_none(
    texts: list[str],
) -> Sequence[tuple[list[int], list[float]] | None] | None:
    """Best-effort sparse leg for a WHOLE document at once — same
    degrade-not-fail shape as `app/agent/tools.py`'s add_note/remember
    helper: a BM25 hiccup costs the document's sparse recall (still
    findable dense-only), not the whole ingest.

    Returns `Sequence` rather than `list` so `embed_sparse_batch`'s
    `list[tuple[...]]` can be returned as-is on success — `list` is
    invariant (mypy can't treat `list[X]` as `list[X | None]`), `Sequence`
    is covariant; the one caller only reads this by index."""
    try:
        return embed_sparse_batch(texts)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sparse embedding unavailable for this ingest; writing dense-only",
            extra={"error_class": type(exc).__name__, "chunk_count": len(texts)},
        )
        return None


async def ingest_text(
    text: str,
    title: str,
    ctx: SecurityCtx | None,
    source: str = "text",
    topic: str | None = None,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> int:
    """Chunk, embed, and upsert `text` as one or more Qdrant points — the
    shared core every `ingest_*` entry point funnels through. Returns the
    number of child chunks written (0 for blank/whitespace text).

    Sparse vectors are computed in one batched call for the whole document
    (local ONNX, ~0.5s for 5700 chunks). Dense vectors — the real
    bottleneck, an HTTP round trip per batch — are embedded
    `EMBED_BATCH_SIZE` chunks at a time so there's a natural per-batch
    checkpoint for progress reporting (~28 checkpoints for 5700 chunks
    instead of one all-or-nothing wait); the ~10x wall-clock win (13min ->
    75s on a real 5700-chunk doc) comes from batching itself
    (`embed_texts`), not from this splitting.

    `on_progress(chunks_embedded, chunks_total)`, if given, is awaited
    after each dense batch (`app/ingestion/ingest_worker.py` uses this for
    the upload UI's progress bar); never called for 0 chunks. Plain
    `async def` callback, awaited directly — no thread-bridging needed
    since this function and `embed_texts`/`qdrant_store.upsert` all run on
    the caller's own event loop.
    """
    if not valid_ctx(ctx):
        metrics.agent_ingest_refused_total.labels(reason="no_ctx").inc()
        raise IngestRefused("a valid tenant+principal ctx is required to ingest content")

    parents = chunk_text(text)
    if not parents:
        return 0

    child_texts = [child_text for parent in parents for child_text in parent.children]
    total = len(child_texts)
    sparse_vectors = _sparse_vectors_or_none(child_texts)

    dense_vectors: list[list[float]] = []
    for start in range(0, total, EMBED_BATCH_SIZE):
        dense_vectors.extend(await embed_texts(child_texts[start : start + EMBED_BATCH_SIZE]))
        if on_progress is not None:
            await on_progress(len(dense_vectors), total)

    points = []
    i = 0
    for parent in parents:
        for child_text in parent.children:
            payload = {
                "text": child_text,
                "parent_id": parent.parent_id,
                "parent_text": parent.text,
                "title": title,
                "source": source,
                "ingested_by": ctx["principal"],
                "kind": "document",
                "tenant": ctx["tenant"],
            }
            if topic:
                payload["topic"] = topic
            points.append(
                qdrant_store.build_point(
                    point_id=uuid.uuid4().hex,
                    dense_vector=dense_vectors[i],
                    payload=payload,
                    sparse_vector=sparse_vectors[i] if sparse_vectors is not None else None,
                )
            )
            i += 1

    await qdrant_store.upsert(points)
    metrics.agent_ingest_total.labels(source=source.split(":")[0]).inc()
    return len(points)


async def ingest_file(path: str, ctx: SecurityCtx | None, topic: str | None = None) -> int:
    """`.txt`/`.md` only, by design — broader formats need a real extraction
    library each (see `app/ingestion/extractors.py`); the pipeline below is
    format-agnostic, so a new format just needs its own `ingest_*` front
    end producing plain text for `ingest_text`."""
    p = Path(path)
    if p.suffix.lower() not in _ALLOWED_FILE_SUFFIXES:
        metrics.agent_ingest_refused_total.labels(reason="bad_file_type").inc()
        raise IngestRefused(
            f"unsupported file type {p.suffix!r} — only {sorted(_ALLOWED_FILE_SUFFIXES)} are supported"
        )
    text = p.read_text(encoding="utf-8", errors="replace")
    return await ingest_text(text, title=p.stem, ctx=ctx, source=f"file:{p.name}", topic=topic)


class _TextExtractor(html.parser.HTMLParser):
    """Minimal, dependency-free HTML→text (stdlib only — no BeautifulSoup):
    collects text nodes, skipping `<script>`/`<style>` content. Not a full
    HTML-to-Markdown converter — just enough to strip markup before
    chunking/embedding, which is all ingestion needs."""

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n\n".join(self._chunks)


def _assert_safe_url(url: str) -> None:
    """SSRF guard — the actual check lives in `app/core/url_safety.py`,
    shared with `app/ingestion/web_crawler.py`'s render path. This wrapper
    just translates `UnsafeURLError` into this module's `IngestRefused` and
    records the refusal metric; see `url_safety.py` for the disclosed
    DNS-rebinding gap."""
    try:
        _assert_safe_url_impl(url)
    except UnsafeURLError as exc:
        metrics.agent_ingest_refused_total.labels(reason="ssrf_blocked").inc()
        raise IngestRefused(str(exc)) from exc


async def ingest_url(url: str, ctx: SecurityCtx | None, topic: str | None = None) -> int:
    """Fetch `url` (SSRF-guarded — see `_assert_safe_url`), strip HTML if
    present, and ingest the result. `follow_redirects=False`: a validated
    URL that redirects to an unvalidated one would otherwise reintroduce
    the exact SSRF surface the guard exists to close."""
    _assert_safe_url(url)
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=_URL_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url, headers={"User-Agent": "agent-core-demo-ingestor/1.0"}
            )
    except httpx.HTTPError as exc:
        metrics.agent_ingest_refused_total.labels(reason="fetch_failed").inc()
        raise IngestRefused(f"fetch failed: {exc}") from exc

    if response.status_code >= 400:
        metrics.agent_ingest_refused_total.labels(reason="fetch_failed").inc()
        raise IngestRefused(f"fetch failed with status {response.status_code}")
    if len(response.content) > _MAX_URL_BYTES:
        metrics.agent_ingest_refused_total.labels(reason="too_large").inc()
        raise IngestRefused(f"response exceeds the {_MAX_URL_BYTES}-byte fetch limit")

    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        extractor = _TextExtractor()
        extractor.feed(response.text)
        text = extractor.text()
    else:
        text = response.text

    return await ingest_text(text, title=url, ctx=ctx, source=f"url:{url}", topic=topic)
