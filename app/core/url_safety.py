"""Shared SSRF guard for anything fetching a caller- or agent-supplied URL:
app/ingestion/ingestor.py::ingest_url and app/ingestion/web_crawler.py's
crawl4ai-backed render. One implementation so the two don't drift.

https-only; every A/AAAA record the hostname resolves to must be a globally
routable address, checked against ALL of them so a mixed public+private record
set is still refused. It is an allow-list (`ipaddress.is_global`), not a list of
known-bad ranges: the earlier list (private/loopback/link-local/reserved/
multicast/unspecified) missed `100.64.0.0/10`, carrier-grade NAT, which holds
cloud metadata services too (`100.100.100.200` is Alibaba Cloud's). The old
flags are still checked as well, so a Python version whose `is_global` differs
can only ever refuse MORE, never less.

Two entry points over one classification: `assert_safe_url_async` for async
callers (both production callers are) and `assert_safe_url` for sync ones. Name
resolution is blocking `getaddrinfo`; called straight from a coroutine it froze
the whole event loop for as long as the lookup took (measured: a 0.5 s resolver
stalled a concurrent heartbeat for 0.51 s), so the async entry point resolves
through the loop's own resolver, which runs it on a worker thread.

Disclosed limitation: this validates resolution now, but the caller (httpx /
headless browser) resolves and connects separately a moment later — a
DNS-rebinding attack could slip through that gap. Closing it fully means pinning
the connection to the address validated here, real added complexity neither
caller takes on. Also not bounded here: how long a lookup may take (it no longer
blocks the loop, but a hung resolver holds the calling task and a worker thread).
"""
import asyncio
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


def _hostname_or_refuse(url: str) -> str:
    """The scheme/host half of the guard — everything that can be decided
    before any lookup, so a refused URL never costs a DNS query."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeURLError(f"only https:// URLs are allowed, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")
    return parsed.hostname


def _check_resolved_addresses(addr_info) -> None:
    """The address half: refuse unless EVERY resolved record is globally
    routable. Shared by both entry points so they cannot disagree."""
    for _family, _type, _proto, _canonname, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        # `::ffff:a.b.c.d` is the IPv4 address a.b.c.d, and a connection to it
        # goes there — judge the address it stands for, not the IPv6 wrapper
        # (whose flags differ between Python versions).
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeURLError(f"URL resolves to a disallowed address ({ip}) — refused")


def assert_safe_url(url: str) -> None:
    """Raises UnsafeURLError if `url` is unsafe to fetch; returns None
    (does nothing) otherwise. Callers must call this BEFORE the actual
    fetch/render, never after.

    BLOCKS while the name resolves — fine from a script or a worker thread,
    never from a coroutine; use `assert_safe_url_async` there."""
    hostname = _hostname_or_refuse(url)
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host {hostname!r}: {exc}") from exc
    _check_resolved_addresses(addr_info)


async def assert_safe_url_async(url: str) -> None:
    """`assert_safe_url` for async callers: identical verdicts, but the name is
    resolved on the loop's resolver thread so a slow lookup cannot freeze every
    other turn, stream and health check running on this event loop."""
    hostname = _hostname_or_refuse(url)
    try:
        addr_info = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host {hostname!r}: {exc}") from exc
    _check_resolved_addresses(addr_info)
