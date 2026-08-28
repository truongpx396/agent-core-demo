"""PDF/DOCX → plain text (production ingestion pipeline). Each extractor
here is deliberately NOT wired into
app/ingestion/ingestor.py::ingest_file's `.txt`/`.md` gate — that scope line was
documented there as intentional, not an oversight ("adding a format later
means adding one more `ingest_*` front end that produces plain text and
calls `ingest_text`, not touching this pipeline"). These are exactly that:
a new front end, feeding the SAME chunk/embed/upsert core every other
ingest path already shares.

Both extractors work on in-memory bytes (never a filesystem path) — the
production upload flow's document lives in MinIO
(app/ingestion/object_store.py::download_bytes), not on the ingest worker's local
disk, so nothing here ever needs one.
"""
import io
import logging

from docx import Document
from pypdf import PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)


class ExtractionFailed(Exception):
    """A file that claims to be a PDF/DOCX but isn't a valid one (or is
    encrypted, or otherwise unreadable) — an expected, caller-facing
    outcome (app/ingestion/ingest_worker.py reports this as a normal job failure),
    not a bug to let propagate as some other exception's inscrutable
    traceback."""


def extract_pdf_text(data: bytes) -> str:
    """Page text, joined with blank lines between pages (the same
    separator app/ingestion/chunking.py's paragraph-aware splitter already expects
    between distinct sections) — pypdf's extract_text() is a best-effort,
    layout-approximate extraction (real PDF text positioning has no
    inherent reading order), not a perfect one; good enough for this
    pipeline's purpose (retrievable, citable chunks), same honest-scope
    posture as app/ingestion/ingestor.py's own HTML-to-text stripping."""
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
    """Paragraph text only (matches python-docx's own document model) —
    tables/headers/footers/embedded objects are out of scope, the same
    "good enough for retrievable chunks, not a perfect converter" line
    extract_pdf_text draws above."""
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises a mix of
        # zipfile/lxml/its own exception types for "not a real docx"; any
        # of them means the same thing to this pipeline: refuse the file.
        raise ExtractionFailed(f"could not parse DOCX: {exc}") from exc
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


EXTRACTORS_BY_SUFFIX = {
    ".pdf": extract_pdf_text,
    ".docx": extract_docx_text,
}
