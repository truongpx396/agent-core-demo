"""General-purpose Ingestor: files, URLs, or raw text → retrievable,
citable Qdrant points — closing the "standalone product needs a way to
build its own corpus, not just answer over a hardcoded one" gap
(GRAPH_PATTERNS.md pattern 24). `app/ingest.py` (`make ingest`) is now
just this module's `ingest_text` called once per sample doc, not a
separate write path — one ingest pipeline, one place it can drift.

Every ingested item is chunked (`app/chunking.py`'s parent-child, sliding-
window strategy) and embedded exactly the way `hybrid_search` expects
(dense + sparse, via `app/embeddings.py`, written through
`qdrant_store.build_point` — the same hybrid vector shape `add_note`/
`remember` already use), so ingested content is retrievable through the
SAME retrieval path as everything else, not a second one.

Every ingested item is stamped with the `tenant` (and, for provenance,
`principal`) from a `SecurityCtx` and refused without one — content whose
ownership can't be established is refused, never ingested as
tenant-less/public (mirrors app/security.py's fail-closed discipline: a
missing ctx is a refusal, not a default).
"""
import html.parser
import ipaddress
import logging
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app import metrics, qdrant_store
from app.chunking import chunk_text
from app.embeddings import embed_sparse, embed_text
from app.security import SecurityCtx, valid_ctx

logger = logging.getLogger(__name__)

_ALLOWED_FILE_SUFFIXES = {".txt", ".md"}
_MAX_URL_BYTES = 2_000_000  # 2 MB — bounded fetch size, part of the SSRF/DoS guard
_URL_TIMEOUT_SECONDS = 10


class IngestRefused(Exception):
    """A refused ingest (bad ctx, disallowed file type, SSRF-blocked URL,
    fetch too large, ...) — an expected, caller-facing outcome, not a bug.
    Every raise site also records `agent_ingest_refused_total{reason=...}`
    (app/metrics.py) so refusals are visible in aggregate, not just to
    whichever caller happened to catch the exception."""


def _sparse_vector_or_none(text: str) -> tuple[list[int], list[float]] | None:
    """Best-effort sparse leg for an ingested chunk — same degrade-not-fail
    shape as app/tools.py's identical helper for add_note/remember: a local
    BM25 model hiccup must not block an ingest, just cost it the sparse
    leg's recall (the point still writes, findable dense-only)."""
    try:
        return embed_sparse(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "sparse embedding unavailable for an ingested chunk; writing dense-only",
            extra={"error_class": type(exc).__name__},
        )
        return None


def ingest_text(
    text: str,
    title: str,
    ctx: SecurityCtx | None,
    source: str = "text",
    topic: str | None = None,
) -> int:
    """Chunk, embed, and upsert `text` as one or more Qdrant points — the
    shared core every `ingest_*` entry point below funnels through.
    Returns the number of child chunks written (0 for blank/whitespace
    text — not an error, there's simply nothing to index).
    """
    if not valid_ctx(ctx):
        metrics.agent_ingest_refused_total.labels(reason="no_ctx").inc()
        raise IngestRefused("a valid tenant+principal ctx is required to ingest content")

    parents = chunk_text(text)
    if not parents:
        return 0

    points = []
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
                    dense_vector=embed_text(child_text),
                    payload=payload,
                    sparse_vector=_sparse_vector_or_none(child_text),
                )
            )

    qdrant_store.upsert(points)
    metrics.agent_ingest_total.labels(source=source.split(":")[0]).inc()
    return len(points)


def ingest_file(path: str, ctx: SecurityCtx | None, topic: str | None = None) -> int:
    """`.txt`/`.md` only, by design: broader formats (PDF, DOCX, ...) need
    a real extraction library each, which is a deliberate scope line for
    this demo's Ingestor, not an oversight — the chunking/embedding/upsert
    pipeline below is format-agnostic, so adding a format later means
    adding one more `ingest_*` front end that produces plain text and
    calls `ingest_text`, not touching this pipeline."""
    p = Path(path)
    if p.suffix.lower() not in _ALLOWED_FILE_SUFFIXES:
        metrics.agent_ingest_refused_total.labels(reason="bad_file_type").inc()
        raise IngestRefused(
            f"unsupported file type {p.suffix!r} — only {sorted(_ALLOWED_FILE_SUFFIXES)} are supported"
        )
    text = p.read_text(encoding="utf-8", errors="replace")
    return ingest_text(text, title=p.stem, ctx=ctx, source=f"file:{p.name}", topic=topic)


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
    """SSRF guard: https-only, and every A/AAAA record the hostname
    resolves to must be public/routable — checked against ALL resolved
    addresses, not just the first, so a hostname with a mixed public+
    private record set still gets refused. A real, non-hollow check
    (AR-035's "no hollow defaults"), not a no-op — but with one honestly
    disclosed limitation: this validates resolution now and lets httpx
    resolve and connect separately, a moment later. A classic DNS-
    rebinding attack (the name resolving differently at actual connect
    time) could still slip through that gap. Closing it fully means
    pinning the connection to the address already validated here (a
    custom transport) — real added complexity for a demo ingestor, so
    this draws the line at "real check, disclosed gap" rather than
    quietly shipping the narrower guard as if it were airtight.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        metrics.agent_ingest_refused_total.labels(reason="ssrf_blocked").inc()
        raise IngestRefused(f"only https:// URLs are allowed, got {parsed.scheme!r}")
    if not parsed.hostname:
        metrics.agent_ingest_refused_total.labels(reason="ssrf_blocked").inc()
        raise IngestRefused("URL has no hostname")

    try:
        addr_info = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        metrics.agent_ingest_refused_total.labels(reason="ssrf_blocked").inc()
        raise IngestRefused(f"could not resolve host {parsed.hostname!r}: {exc}")

    for _family, _type, _proto, _canonname, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            metrics.agent_ingest_refused_total.labels(reason="ssrf_blocked").inc()
            raise IngestRefused(f"URL resolves to a disallowed address ({ip}) — refused")


def ingest_url(url: str, ctx: SecurityCtx | None, topic: str | None = None) -> int:
    """Fetch `url` (SSRF-guarded — see `_assert_safe_url`), strip HTML if
    present, and ingest the result. `follow_redirects=False`: a validated
    URL that redirects to an unvalidated one would otherwise reintroduce
    the exact SSRF surface the guard exists to close."""
    _assert_safe_url(url)
    try:
        with httpx.Client(follow_redirects=False, timeout=_URL_TIMEOUT_SECONDS) as client:
            response = client.get(
                url, headers={"User-Agent": "practice-core-ai-1-ingestor/1.0"}
            )
    except httpx.HTTPError as exc:
        metrics.agent_ingest_refused_total.labels(reason="fetch_failed").inc()
        raise IngestRefused(f"fetch failed: {exc}")

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

    return ingest_text(text, title=url, ctx=ctx, source=f"url:{url}", topic=topic)
