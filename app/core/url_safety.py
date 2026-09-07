"""Shared SSRF guard for anything in this app that fetches a caller- or
agent-supplied URL: app/ingestion/ingestor.py::ingest_url (the original,
ingest-specific caller) and app/ingestion/web_crawler.py's crawl4ai-backed
render (added for domain tools that fetch a live web page mid-turn, e.g.
app/domains/sales/tools.py::enrich_lead_from_website). One implementation,
not two independently-maintained SSRF checks that can drift out of sync —
the same "one place it can drift" reasoning app/domains/notify.py already
gives for sharing one team-channel notifier across three domains.

https-only, and every A/AAAA record the hostname resolves to must be
public/routable — checked against ALL resolved addresses, not just the
first, so a hostname with a mixed public+private record set still gets
refused. Disclosed limitation (unchanged from before this was extracted
out of app/ingestion/ingestor.py): this validates resolution now and lets
the caller (httpx, or a headless browser) resolve and connect separately,
a moment later — a DNS-rebinding attack could still slip through that gap.
Closing it fully means pinning the actual connection to the address
already validated here (a custom transport / browser proxy), real added
complexity neither caller takes on today.
"""
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(Exception):
    """A URL that fails the SSRF guard — non-https, no hostname,
    unresolvable, or resolving to a private/loopback/link-local/reserved/
    multicast/unspecified address. Callers translate this into their own
    domain-facing refusal (see app/ingestion/ingestor.py::IngestRefused)
    rather than this module raising something ingest-specific itself — a
    headless-browser crawl (app/ingestion/web_crawler.py) is refused for
    exactly the same reason but isn't an "ingest" at all."""


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
