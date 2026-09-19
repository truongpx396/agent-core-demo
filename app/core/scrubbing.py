"""Scrubs credential-shaped values out of TOOL OUTPUT before it reaches a
prompt, trace, or audit entry (pattern 32) — a different chokepoint than a
model-call-path PII scrubber, since tool output (a raw DB row, file
contents, an API response) reaches the LLM/Langfuse directly as
`ToolMessage.content`, never through the model-call path.

Two layers: (1) static patterns for common credential shapes (`sk-...`
keys, AWS key ids, `password=`/`token=`/`secret=`/`api_key=` pairs, a
URL's embedded `user:password@`, JWT-shaped strings); (2) this
deployment's own bound secret values, read live from `app.core.config`, so
an exact echo of a real configured secret is caught even when it doesn't
match a generic pattern (e.g. a `query_employees` row surfacing a raw
connection string).

Always on, no config flag — tool output is never trusted to be
credential-free by default.
"""
import logging
import re

logger = logging.getLogger(__name__)

_REDACTED = "[REDACTED]"

_STATIC_PATTERNS = [
    re.compile(p)
    for p in [
        r"sk-[A-Za-z0-9]{16,}",  # OpenAI-style API key
        r"AKIA[0-9A-Z]{16}",  # AWS access key id
        r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+",
        r"://[^:\s/@]+:[^@\s/]+@",  # scheme://user:PASSWORD@host in a URL
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",  # JWT-shaped
    ]
]


def _bound_secret_values() -> list[str]:
    """Read fresh from app.core.config on every call (not cached at import
    time) so a runtime config change is honored. Filtered to non-empty
    strings: an unset secret defaults to `""`, and scrubbing `""` would
    mangle every character boundary via `str.replace`."""
    from app.core import config

    candidates = [
        config.OPENAI_API_KEY,
        config.LANGFUSE_SECRET_KEY,
        config.LANGFUSE_PUBLIC_KEY,
    ]
    for url in (config.APPDATA_DATABASE_URL, config.REDIS_URL):
        # Password only — the host/db name is harmless and useful in a trace.
        match = re.search(r"://[^:\s/@]+:([^@\s/]+)@", url)
        if match:
            candidates.append(match.group(1))
    return [c for c in candidates if c]


def scrub(text: str) -> str:
    """Returns `text` with every credential-shaped or bound secret value
    replaced by `[REDACTED]`. Never raises — on unexpected failure this
    degrades to returning `text` UNSCRUBBED (logged), rather than blocking
    the tool result, same posture as moderation.py::screen."""
    if not text:
        return text
    try:
        scrubbed = text
        for pattern in _STATIC_PATTERNS:
            scrubbed = pattern.sub(_REDACTED, scrubbed)
        for secret in _bound_secret_values():
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, _REDACTED)
        return scrubbed
    except Exception:  # noqa: BLE001
        logger.warning("secret scrubbing failed; returning tool output unscrubbed")
        return text
