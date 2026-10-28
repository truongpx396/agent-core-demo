"""How much time does the front door's check add to a request? `python -m scripts.authlimit_latency` (specs/018, SC-008).

The comparison is built to isolate the check and nothing else: the same client, the same TLS connection, the same Caddy and the same stub
upstream, alternating one request to a CHECKED route (Caddy asks authlimit first) with one to a PUBLIC route (Caddy goes straight to the
upstream). Alternating keeps the two samples under the same load, temperature and garbage-collection weather; the difference is the
`forward_auth` hop plus authlimit's work (verify the token, look up revocation, charge the buckets, sign an identity).

The proposed targets (spec 018 SC-008) were: median added under 10 ms, 95th percentile added under 50 ms. This prints whether they were met;
the numbers belong in specs/018-authlimit-edge-auth/research.md R6. It measures a development stack on one machine, through Docker Desktop's
network: a floor for the cost of the design, not a production figure.

A route that is LIMITED (a turn) spends tokens and answers 429 once the bucket is empty, which would be measuring the refusal. Measure the
limiter's cost with the stack in observe mode (`AUTHLIMIT_LIMIT_MODE=observe`, which still calls Redis but never refuses), or measure a
route that is checked and never limited, the default.
"""
import argparse
import asyncio
import os
import ssl
import statistics
import sys
import time

import httpx

from scripts.authlimit_smoke import Door


def percentile(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(round(p / 100 * (len(sorted_ms) - 1))))]


def summarise(name: str, ms: list[float]) -> dict[str, float]:
    s = sorted(ms)
    stats = {"median": statistics.median(s), "p95": percentile(s, 95), "p99": percentile(s, 99), "max": s[-1]}
    print(f"  {name:<28} median {stats['median']:7.2f} ms   p95 {stats['p95']:7.2f} ms   p99 {stats['p99']:7.2f} ms   max {stats['max']:7.2f} ms   (n={len(s)})")
    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the time the front door's check adds (specs/018, SC-008).")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--api", default="https://api.localhost:8443")
    parser.add_argument("--auth", default="https://auth.localhost:8443")
    parser.add_argument("-n", type=int, default=500, help="requests to EACH route")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--path", default="/chat/sessions", help="the CHECKED route to measure (default: verified, never limited)")
    parser.add_argument("--baseline", default="/health", help="the PUBLIC route to compare against")
    args = parser.parse_args()
    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("CASDOOR_ADMIN_PASSWORD is not set")

    tls = ssl.create_default_context(cafile=args.ca)
    door = Door(argparse.Namespace(api=args.api, mcp="https://mcp.localhost:8443", auth=args.auth, compose=""), tls, password)
    await door.start()
    try:
        user = await door.user("ecorp")
        token = await door.token(user)
        if not token:
            sys.exit("could not sign in")
        checked, public, odd = [], [], 0
        async with httpx.AsyncClient(verify=tls, timeout=20.0) as client:
            auth = {"Authorization": f"Bearer {token}"}
            for _ in range(50):  # warm up: the TLS connection, Caddy's upstream pool, the JIT-less but cache-hungry Go runtime
                await client.request(args.method, args.api + args.path, headers=auth)
                await client.get(args.api + args.baseline)
            for _ in range(args.n):
                t0 = time.perf_counter()
                r1 = await client.request(args.method, args.api + args.path, headers=auth)
                t1 = time.perf_counter()
                r2 = await client.get(args.api + args.baseline)
                t2 = time.perf_counter()
                if r1.status_code != 200 or r2.status_code != 200:
                    odd += 1  # a refusal measures the refusal, not the check: leave it out and say so
                    continue
                checked.append((t1 - t0) * 1000)
                public.append((t2 - t1) * 1000)
        if odd:
            print(f"  note: {odd} of {args.n} pairs were not both 200 (limited? use observe mode for limited routes) and were left out")
        if len(checked) < 50:
            sys.exit("too few usable samples")
        print(f"\n{args.method} {args.path} (checked)  vs  GET {args.baseline} (public), same connection, alternating:\n")
        a = summarise("checked", checked)
        b = summarise("public (no check)", public)
        added_median = a["median"] - b["median"]
        added_p95 = a["p95"] - b["p95"]
        print(f"\n  added by the check:  median {added_median:+.2f} ms   p95 {added_p95:+.2f} ms")
        ok = added_median < 10 and added_p95 < 50
        print(f"  targets (median < 10 ms, p95 < 50 ms): {'MET' if ok else 'NOT MET'}")
        return 0 if ok else 1
    finally:
        await door.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
