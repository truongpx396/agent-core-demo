"""Scrubs credential-shaped values out of TOOL OUTPUT before it reaches a
prompt, a trace, or an audit entry (GRAPH_PATTERNS.md pattern 32) — a
different chokepoint than a model-call-path PII scrubber would be,
because tool output never passes through the model-call path: a tool can
return a raw database row, a file's contents, or an API response
containing a credential nobody asked the model to reveal, and that text
reaches the LLM (and Langfuse) directly as `ToolMessage.content`.

Two independent layers:
1. **Static patterns** — common credential shapes (OpenAI-style `sk-...`
   keys, AWS access key ids, `password=`/`token=`/`secret=`/`api_key=`
   pairs, a connection URL's embedded `user:password@`, JWT-shaped
   strings).
2. **This deployment's own bound secret values** — the actual configured
   values (`OPENAI_API_KEY`, `LANGFUSE_SECRET_KEY`/`_PUBLIC_KEY`, the
   password portion of `APPDATA_DATABASE_URL`/`REDIS_URL`) read from
   `app.core.config` at call time, so an exact echo of THIS deployment's real
   secret is caught even when it doesn't match any generic pattern —
   static patterns alone don't catch a structured tool result (e.g. a
   `query_employees` row) that happens to surface a column containing a
   raw connection string.

Always on, no config flag — a tool result is never trusted to be
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
    """This deployment's own actual configured secret values — read
    fresh from app.core.config on every call (not cached at import time) so a
    runtime config change is honored. Filtered to non-empty strings only:
    an unset secret is `""` in this app's Settings defaults, and scrubbing
    `""` would (via `str.replace`) mangle every character boundary of
    every tool result."""
    from app.core import config

    candidates = [
        config.OPENAI_API_KEY,
        config.LANGFUSE_SECRET_KEY,
        config.LANGFUSE_PUBLIC_KEY,
    ]
    for url in (config.APPDATA_DATABASE_URL, config.REDIS_URL):
        # Only the password portion — scrubbing the whole URL would also
        # hide the host/db name, which is harmless and often useful to see
        # in a trace (e.g. "which Postgres did this hit").
        match = re.search(r"://[^:\s/@]+:([^@\s/]+)@", url)
        if match:
            candidates.append(match.group(1))
    return [c for c in candidates if c]


def scrub(text: str) -> str:
    """Returns `text` with every credential-shaped or actually-bound
    secret value replaced by `[REDACTED]`. Never raises — a scrubbing bug
    must not be able to crash a tool call; on any unexpected failure this
    degrades to returning `text` UNSCRUBBED (logged as a warning) rather
    than blocking the tool result entirely, the same "a safety check's
    own failure must not itself crash the turn" posture
    app/agent/moderation.py::screen already takes.
    """
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
