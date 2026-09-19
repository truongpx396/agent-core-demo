"""PDF/DOCX → plain text (production ingestion pipeline). Deliberately not
wired into `ingestor.py::ingest_file`'s `.txt`/`.md` gate — these are a new
front end (one per format) feeding the same chunk/embed/upsert core every
ingest path shares, per that function's own documented scope line.

Both extractors work on in-memory bytes, never a filesystem path — the
upload flow's document lives in MinIO (`object_store.py::download_bytes`),
not on the ingest worker's local disk.
"""
import io
import logging

from docx import Document
from pypdf import PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)


class ExtractionFailed(Exception):
    """A file that claims to be a PDF/DOCX but isn't valid (encrypted,
    corrupt, unreadable) — an expected, caller-facing outcome
    (`ingest_worker.py` reports it as a normal job failure), not a bug."""


def extract_pdf_text(data: bytes) -> str:
    """Page text joined with blank lines (the separator `chunking.py`'s
    paragraph-aware splitter expects between sections). `pypdf.extract_text()`
    is best-effort/layout-approximate, not perfect — good enough for
    retrievable, citable chunks, same scope as `ingestor.py`'s HTML stripping."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionFailed("PDF is password-protected")
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ExtractionFailed:
        raise
    except (PdfReadError, ValueError) as exc:
        raise ExtractionFailed(f"could not parse PDF: {exc}") from exc


def extract_docx_text(data: bytes) -> str:
    """Paragraph text only — tables/headers/footers/embedded objects are
    out of scope, same "good enough, not a perfect converter" line as
    `extract_pdf_text`."""
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises a mix of
        # exception types for "not a real docx"; all mean the same thing here.
        raise ExtractionFailed(f"could not parse DOCX: {exc}") from exc
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


EXTRACTORS_BY_SUFFIX = {
    ".pdf": extract_pdf_text,
    ".docx": extract_docx_text,
}
