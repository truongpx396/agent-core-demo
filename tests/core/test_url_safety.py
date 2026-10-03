"""The shared SSRF guard (`app/core/url_safety.py`) in front of every fetch of a
caller- or agent-supplied URL: `ingest_url` and the headless-browser crawl.

Two defects were found and reproduced before this file existed (spec 009):

  * B26 — the guard refused an address only if it was private, loopback,
    link-local, reserved, multicast or unspecified. `100.64.0.0/10` (carrier-grade
    NAT, "shared address space", RFC 6598) is none of those, so it was allowed —
    and `100.100.100.200` is Alibaba Cloud's instance-metadata address, the same
    class of target as `169.254.169.254`. The check is now an allow-list:
    only a *globally routable* address passes.
  * B27 — `assert_safe_url` resolves the host with the blocking
    `socket.getaddrinfo`, and both callers are `async def` and called it
    directly, so one slow DNS answer froze the whole event loop (every other
    turn, stream and health check on that worker) for as long as it took.
    Measured: a 0.5 s resolver stalled a concurrent heartbeat for 0.51 s.

Classification is tested through a patched resolver so the result does not
depend on the machine's DNS or on how its libc spells an address; a few IP-literal
hosts are also run through the real resolver, which needs no network.
"""
import asyncio
import socket
import time

import pytest

from app.core.url_safety import UnsafeURLError, assert_safe_url, assert_safe_url_async


def _resolves_to(monkeypatch, *addresses: str) -> None:
    """Make every host resolve to exactly these addresses (one record each)."""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# The spec's probe table (data-model §6) plus the carrier-grade-NAT range.
_REFUSED = [
    "127.0.0.1",
    "::1",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",  # IPv4-mapped private
    "169.254.169.254",  # link-local: AWS/GCP/Azure metadata
    "192.0.0.192",  # IETF protocol assignments (Oracle metadata)
    "fe80::1",
    "64:ff9b::7f00:1",  # NAT64 -> loopback
    "64:ff9b::a9fe:a9fe",  # NAT64 -> metadata
    "2002:7f00:1::",  # 6to4 -> loopback
    "fd00:ec2::254",  # AWS IPv6 metadata (unique-local)
    "100.64.0.1",  # carrier-grade NAT, bottom of 100.64.0.0/10  (B26)
    "100.100.100.200",  # Alibaba Cloud metadata                     (B26)
    "100.127.255.254",  # top of 100.64.0.0/10                       (B26)
    "::ffff:100.64.0.1",  # the same range, spelled IPv4-mapped      (B26)
    "198.18.0.1",  # benchmarking range
    "240.0.0.1",  # reserved
    "0.0.0.0",
    "224.0.0.1",  # multicast
    "ff02::1",  # IPv6 multicast
]

_ALLOWED = ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"]


@pytest.mark.parametrize("address", _REFUSED)
def test_an_address_that_is_not_globally_routable_is_refused(address, monkeypatch):
    _resolves_to(monkeypatch, address)

    with pytest.raises(UnsafeURLError):
        assert_safe_url("https://target.example/")


@pytest.mark.parametrize("address", _ALLOWED)
def test_a_public_address_is_allowed(address, monkeypatch):
    _resolves_to(monkeypatch, address)

    assert_safe_url("https://target.example/")  # must not raise


def test_a_host_with_one_public_and_one_disallowed_record_is_refused(monkeypatch):
    """The guard checks EVERY record: the connection may use any of them."""
    _resolves_to(monkeypatch, "8.8.8.8", "100.100.100.200")

    with pytest.raises(UnsafeURLError):
        assert_safe_url("https://target.example/")


@pytest.mark.parametrize("url", ["https://100.64.0.1/", "https://100.100.100.200/", "https://127.0.0.1/", "https://[::1]/"])
def test_ip_literal_hosts_are_refused_through_the_real_resolver(url):
    with pytest.raises(UnsafeURLError):
        assert_safe_url(url)


@pytest.mark.parametrize(
    ("url", "reason"),
    [("http://example.com/", "https"), ("https:///path", "hostname"), ("ftp://example.com/", "https")],
)
def test_a_non_https_url_or_a_missing_host_is_refused_before_any_lookup(url, reason, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not resolve a URL that already failed the scheme/host check")

    monkeypatch.setattr(socket, "getaddrinfo", boom)

    with pytest.raises(UnsafeURLError, match=reason):
        assert_safe_url(url)


def test_an_unresolvable_host_is_refused(monkeypatch):
    def nxdomain(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)

    with pytest.raises(UnsafeURLError, match="could not resolve"):
        assert_safe_url("https://no-such-host.invalid/")


# --- the async entry point (B27) ------------------------------------------------


@pytest.mark.parametrize("address", _REFUSED)
async def test_the_async_guard_refuses_exactly_what_the_sync_guard_refuses(address, monkeypatch):
    _resolves_to(monkeypatch, address)

    with pytest.raises(UnsafeURLError):
        await assert_safe_url_async("https://target.example/")


@pytest.mark.parametrize("address", _ALLOWED)
async def test_the_async_guard_allows_a_public_address(address, monkeypatch):
    _resolves_to(monkeypatch, address)

    await assert_safe_url_async("https://target.example/")  # must not raise


async def test_the_async_guard_refuses_a_non_https_url_without_resolving(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not resolve a URL that already failed the scheme/host check")

    monkeypatch.setattr(socket, "getaddrinfo", boom)

    with pytest.raises(UnsafeURLError, match="https"):
        await assert_safe_url_async("http://example.com/")


async def test_the_async_guard_refuses_an_unresolvable_host(monkeypatch):
    def nxdomain(*args, **kwargs):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", nxdomain)

    with pytest.raises(UnsafeURLError, match="could not resolve"):
        await assert_safe_url_async("https://no-such-host.invalid/")


_SLOW_RESOLVE_SECONDS = 0.5


def _slow_public_resolver(monkeypatch) -> None:
    def slow(host, port, *args, **kwargs):
        time.sleep(_SLOW_RESOLVE_SECONDS)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow)


async def _longest_gap_between_ticks(work) -> float:
    """Run `work()` while a task ticks every 10 ms; return the longest time the
    loop went without running that task. A loop kept free ticks on schedule; a
    loop blocked inside `work` shows a gap of about the block's length."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # let the heartbeat establish its rhythm
    await work()
    stop.set()
    await task
    return max(gaps)


async def test_the_async_guard_does_not_stall_the_event_loop_while_a_name_resolves(monkeypatch):
    _slow_public_resolver(monkeypatch)

    gap = await _longest_gap_between_ticks(lambda: assert_safe_url_async("https://target.example/"))

    assert gap < _SLOW_RESOLVE_SECONDS * 0.8, (  # a free loop ticks in ~10 ms; margin is for a loaded runner
        f"the loop was unresponsive for {gap:.2f}s while a name resolved — name resolution is blocking it"
    )


async def test_control_the_sync_guard_called_from_async_code_does_stall_the_loop(monkeypatch):
    """Why the async entry point exists, and proof the measurement above can see
    a stall at all: the sync function, called straight from a coroutine, holds
    the loop for the full resolution time."""
    _slow_public_resolver(monkeypatch)

    async def call_sync():
        assert_safe_url("https://target.example/")

    gap = await _longest_gap_between_ticks(call_sync)

    assert gap >= _SLOW_RESOLVE_SECONDS * 0.9

