"""The hardware profile a latency number belongs to, and memory measurement (specs/014 FR-002, FR-004).

A latency without its hardware is not comparable: the first benchmark in this work reported a figure that was 2.7x too high for the CPU
it was meant to describe, partly because a quantized file that is fast on x86 was slow on this ARM chip. So every report carries a
profile and an id, a baseline keys its latency by that id, and a comparison across ids drops the latency delta and says so.
Quality metrics do not depend on hardware and are always compared.
"""
import hashlib
import json
import os
import platform
import resource
import subprocess
import sys
from importlib import metadata


def _cpu_brand() -> str:
    try:
        if sys.platform == "darwin":
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=5).strip()
        if sys.platform.startswith("linux"):
            for line in open("/proc/cpuinfo", encoding="utf-8"):  # noqa: SIM115 - a small read, closed on garbage collection
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return platform.processor() or platform.machine() or "unknown"


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def profile(threads: int | None = None) -> dict:
    """{id, cpu, cores, threads, onnxruntime, os}. `threads` is the ONNX intra-op thread count the run used (default: all cores)."""
    cores = os.cpu_count() or 1
    body = {
        "cpu": _cpu_brand(),
        "cores": cores,
        "threads": threads if threads is not None else cores,
        "onnxruntime": _version("onnxruntime"),
        "os": f"{platform.system()} {platform.release()}",
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:8]
    return {"id": f"hw-{digest}", **body}


def rss_mb() -> float:
    """Resident set size of this process now, in MB (`ps`, so it is the same unit on macOS and Linux)."""
    out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True, timeout=5)
    return int(out.split()[0]) / 1024


def peak_rss_mb() -> float:
    """Peak resident size so far. macOS reports bytes, Linux kilobytes."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
