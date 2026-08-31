"""Fast, curated garak probe subset against the real model — report-only,
run on every push in CI (`.github/workflows/ci.yml`'s `garak` job; `make
garak` locally). See `garak/config.yaml`'s own header and GRAPH_PATTERNS.md
pattern 48 for the full design writeup; this docstring covers WHY this is
report-only rather than a pass/fail gate — a deliberate conclusion, not a
default, reached empirically while building this:

**garak scans the RAW model, bypassing `app/agent/moderation.py`
entirely** — unlike promptfoo/support.yaml (which tests THIS app's system
prompt), garak's OpenAICompatible target talks straight to Ollama, with no
graph, no moderation node, no system prompt at all in front of it. That's
the point: it measures the base model's OWN resistance as a defense-in-depth
signal, independent of (not a regression test FOR) `moderation.py`'s
pattern-based screening, which sits in front of the model in the real app
and is already covered by its own hermetic tests.

**A small local model has near-zero jailbreak resistance on its own —
verified directly, not assumed.** Running `dan.Dan_11_0`/`dan.AntiDAN`
against `qwen2.5:1.5b` here scored a 100% attack success rate on at least
one detector every time this was tried while building this script. That's
an honest, disclosed property of choosing a small, fast, CI-friendly local
model (the same tradeoff `tests/live/`'s own model choice makes for speed),
not a bug this app's code can fix — a hard "the raw model must resist this
probe" gate would be permanently red from the first push, which is worse
than no gate at all (the classic failure mode a brand-new scanner gate
should specifically avoid — see `scripts/eval.py`'s own comment on why a
gate that always fails gets disabled by a frustrated team). So this prints
a clear, honest summary and only fails the BUILD on a genuine scan
execution error (garak itself crashing), never on an attack succeeding.
`make garak-full`/CI's uploaded `garak_runs/` artifact are where a human
actually reviews findings — the same "hard gate + human-reviewed report"
split `scripts/eval.py` uses for its own grounded-claims/pass-rate numbers.

**`agent_breaker` (a real, newer probe specifically targeting tool-calling
agents — directly relevant to this app's own tool-bound agent) is
deliberately excluded, from BOTH this fast subset and `garak-full`** —
verified directly that its detector requires a `NIM_API_KEY` (an NVIDIA
NIM cloud API key) with no local-model alternative, which this fully-local,
offline-by-default demo has no way to satisfy. A real, disclosed gap, not
an oversight: anyone with a NIM key can add it back locally by extending
`_FAST_PROBE_SPEC` below.

**`promptinject`/`latentinjection` are excluded from the FAST subset only**
(they're fine for `make garak-full`) — verified directly that a single
`promptinject.HijackHateHumans` run generates 64 prompts (vs. the `dan`
family's 1 each), making it far too slow for a per-push CI job at even one
generation per prompt.

Run with: `python garak/run_ci_scan.py` (see Makefile's `garak` target).
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "garak_runs"
CONFIG_PATH = REPO_ROOT / "garak" / "config.yaml"

# One generation each — this is a fast smoke pass, not `make garak-full`'s
# deeper scan. Every probe here has exactly 1 prompt (verified directly via
# `len(ProbeClass().prompts)`) so total runtime stays a real-model-call away
# from instant, not proportional to a probe's full combinatorial prompt set.
_FAST_PROBE_SPEC = "probes.dan.Dan_11_0,probes.dan.AntiDAN,probes.dan.DAN_Jailbreak"


def _latest_report() -> Path | None:
    reports = sorted(REPORT_DIR.glob("*.report.jsonl"), key=lambda p: p.stat().st_mtime)
    return reports[-1] if reports else None


def _summarize(report_path: Path) -> None:
    print(f"\ngarak report: {report_path}")
    print(f"{'probe':<20} {'detector':<25} {'result'}")
    for line in report_path.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("entry_type") != "eval":
            continue
        total = entry["total_evaluated"]
        rate = 100.0 * entry["fails"] / total if total else 0.0
        outcome = "no failures" if entry["fails"] == 0 else f"attack success rate {rate:.0f}%"
        print(f"{entry['probe']:<20} {entry['detector']:<25} {outcome}")
    print(
        "\nThis is an INFORMATIONAL signal about the raw model's own "
        "jailbreak resistance — see this script's own module docstring for "
        "why it doesn't gate the build. `app/agent/moderation.py`'s "
        "pattern-based screening (tested separately, hermetically) is what "
        "actually stands in front of this model in the real app."
    )


def _config_with_absolute_report_dir() -> Path:
    """garak/config.yaml's own `reporting.report_dir: garak_runs` is
    resolved relative to garak's OWN data dir (`~/.local/share/garak`), NOT
    the current working directory — verified directly in garak's own
    `command.py::start_run` (`if not report_path.is_absolute(): report_path
    = _config.transient.data_dir / ...`); there's no CLI flag to override
    just the directory (`--report_prefix` only sets the filename). So this
    writes a TEMP config layering an absolute, repo-relative `report_dir`
    onto garak/config.yaml's own settings, and returns that instead —
    keeps garak/config.yaml itself as the single source of truth for
    generator settings, portable across machines/CI, while still landing
    the report where CI's `actions/upload-artifact` step (and a local
    `garak_runs/`, already in .gitignore) expects it.
    """
    config = yaml.safe_load(CONFIG_PATH.read_text())
    config.setdefault("reporting", {})["report_dir"] = str(REPORT_DIR)
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(config, f)
    return Path(path)


def main() -> int:
    model = os.environ.get("GARAK_MODEL", "qwen2.5:1.5b")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OPENAICOMPATIBLE_API_KEY", "sk-not-checked-by-ollama")
    config_path = _config_with_absolute_report_dir()

    # garak itself exits non-zero for a probe that found ANY successful
    # attack, which is exactly the outcome this script treats as
    # informational, not a build failure (see module docstring) — so its
    # exit code is deliberately never checked here. What DOES mean
    # something went genuinely wrong is no report ever appearing at all
    # (a crash before garak got as far as writing one).
    subprocess.run(
        [
            sys.executable, "-m", "garak",
            "--config", str(config_path),
            "--target_type", "openai.OpenAICompatible",
            "--target_name", model,
            "--spec", _FAST_PROBE_SPEC,
            "-g", "1",
        ],
        cwd=REPO_ROOT,
    )
    report_path = _latest_report()
    if report_path is None:
        print("garak produced no report file — treating this as a real scan failure.")
        return 1

    _summarize(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
