"""Shared SSRF guard for anything fetching a caller- or agent-supplied URL:
app/ingestion/ingestor.py::ingest_url and app/ingestion/web_crawler.py's
crawl4ai-backed render. One implementation so the two don't drift.

https-only; every A/AAAA record the hostname resolves to must be
public/routable, checked against ALL of them so a mixed public+private
record set is still refused. Disclosed limitation: this validates
resolution now, but the caller (httpx / headless browser) resolves and
connects separately a moment later — a DNS-rebinding attack could slip
through that gap. Closing it fully means pinning the connection to the
address validated here, real added complexity neither caller takes on.
"""
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(Exception):
    """A URL that fails the SSRF guard — non-https, no hostname,
    unresolvable, or resolving to a private/loopback/link-local/reserved/
    multicast/unspecified address. Callers translate this into their own
    domain-facing refusal (e.g. app/ingestion/ingestor.py::IngestRefused)
    rather than this module raising something ingest-specific — a
    headless-browser crawl is refused the same way but isn't an "ingest"."""


def assert_safe_url(url: str) -> None:
    """Raises UnsafeURLError if `url` is unsafe to fetch; returns None
    (does nothing) otherwise. Callers must call this BEFORE the actual
    fetch/render, never after."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeURLError(f"only https:// URLs are allowed, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")

    try:
        addr_info = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host {parsed.hostname!r}: {exc}") from exc

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
            raise UnsafeURLError(f"URL resolves to a disallowed address ({ip}) — refused")
