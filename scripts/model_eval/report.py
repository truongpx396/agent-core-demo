"""Assembling, rendering and validating the evaluation report (specs/014 contracts/model-eval.md).

The JSON is the record; the Markdown is a view of it. Rules the code enforces because each prevents a wrong decision:
  * A report carries ids and aggregate numbers only, never item text (reports are pasted into PRs).
  * `inconclusive` is a verdict, not equality. It carries `needed_n`, an estimate of how many items would settle it.
  * Latency is compared only against a baseline recorded on the same hardware id; otherwise the latency delta is dropped and a warning
    says so. Quality is always compared.
  * Controls decide whether the report may be used at all: any failed control makes it `invalid`.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from scripts.model_eval import baselines
from scripts.model_eval.controls import (
    ControlResult,
    derived_controls,
    judge_candidate_control,
)
from scripts.model_eval.stats import N_BOOT, SEED, compare, measure
from scripts.model_eval.suites import ItemData, Suite

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "specs/014-model-serving-eval-harness/contracts/model-eval.report.schema.json"


@dataclass
class RunOutput:
    """What a runner hands back: the per-item data plus everything a report says about the candidate and the dataset."""

    kind: str
    candidate: dict  # name, identity, licence, path (+ revision, size_bytes, dim)
    dataset: dict  # name, version, split, items, reviewed_share (+ public)
    data: ItemData
    efficiency: dict  # query_ms {p50, p95}, ms_per_chunk, rss_mb, run_seconds
    warnings: list[str] = field(default_factory=list)


def _tradeoffs(report: dict, baseline: baselines.Baseline | None, hw_id: str) -> list[str]:
    lines: list[str] = []
    deltas = report.get("deltas", {})
    if deltas:
        better = [k for k, d in deltas.items() if d["verdict"] == "better"]
        worse = [k for k, d in deltas.items() if d["verdict"] == "worse"]
        unclear = [k for k, d in deltas.items() if d["verdict"] == "inconclusive"]
        lines.append(
            f"quality: better on {', '.join(better) or 'nothing'}; worse on {', '.join(worse) or 'nothing'}; "
            f"inconclusive on {', '.join(unclear) or 'nothing'}"
        )
    else:
        lines.append("quality: no baseline to compare against; the numbers above stand on their own")
    eff = report["efficiency"]
    base_eff = baseline.efficiency_for(hw_id) if baseline else None
    if base_eff and report["baseline"]["latency_comparable"]:
        lines.append(f"speed: query p50 {eff['query_ms']['p50']:.1f} ms vs baseline {base_eff['query_ms']['p50']:.1f} ms (same hardware)")
    else:
        lines.append(f"speed: query p50 {eff['query_ms']['p50']:.1f} ms, p95 {eff['query_ms']['p95']:.1f} ms; no baseline on this hardware to compare")
    size = report["candidate"].get("size_bytes")
    lines.append(f"size: {size / 1e6:.0f} MB on disk" if size else "size: not measured for this path")
    lines.append(f"licence: {report['candidate']['licence']}")
    dim = report["candidate"].get("dim")
    if report["kind"] == "embedding" and dim:
        lines.append(
            f"embedding: dim {dim} is {dim * 4 / 1e3:.1f} KB per vector, about {dim * 4e6 / 1e9:.1f} GB per million chunks; "
            "changing the embedding model means re-embedding every collection (make reembed)"
        )
    return lines


def build_report(
    run: RunOutput,
    suite: Suite,
    baseline: baselines.Baseline | None,
    *,
    hardware: dict,
    repo_revision: str,
    ran_at: str,
    seed: int = SEED,
    n_boot: int = N_BOOT,
) -> dict:
    if baseline is not None:
        baselines.check_comparable(baseline, run.dataset["name"], run.dataset["version"])
    quality = {name: {"value": mv.value, "ci95": list(mv.ci95)} for name, mv in measure(suite, run.data, seed=seed, n_boot=n_boot).items()}
    warnings = list(run.warnings)
    deltas: dict = {}
    baseline_block = None
    candidate_deltas = None
    if baseline is not None:
        candidate_deltas = compare(suite, run.data, baseline.data, seed=seed, n_boot=n_boot)
        deltas = {k: {"delta": d.delta, "ci95": list(d.ci95), "verdict": d.verdict, "needed_n": d.needed_n} for k, d in candidate_deltas.items()}
        comparable = baseline.efficiency_for(hardware["id"]) is not None
        baseline_block = {
            "name": baseline.name,
            "file": str(baseline.path),
            "hw_used": hardware["id"] if comparable else next(iter(baseline.record.get("efficiency", {})), None),
            "latency_comparable": comparable,
        }
        if not comparable:
            warnings.append("latency is not compared: the baseline was recorded on different hardware; quality is")
        if baseline.record.get("provisional"):
            warnings.append("the baseline is PROVISIONAL (recorded on an unreviewed dataset)")
    controls: list[ControlResult] = derived_controls(run.kind, suite, (baseline.data if baseline else run.data), seed=seed)
    own = judge_candidate_control(run.candidate["name"], run.kind, candidate_deltas) if candidate_deltas else None
    if own is not None:
        controls.append(own)
    reviewed = run.dataset.get("reviewed_share", 0.0)
    if reviewed < 1.0:
        warnings.append(f"PROVISIONAL: {round((1 - reviewed) * 100)}% of the evaluation items are not yet reviewed by a person")
    status = "invalid" if any(not c.ok for c in controls) else ("provisional" if reviewed < 1.0 else "ok")
    if status == "invalid":
        warnings.append("INVALID: a control did not come out as expected, so this report must not be used")
    report = {
        "schema_version": 1,
        "kind": run.kind,
        "status": status,
        "candidate": run.candidate,
        "baseline": baseline_block,
        "dataset": run.dataset,
        "hardware": {k: hardware[k] for k in ("id", "cpu", "cores", "threads", "onnxruntime", "os")},
        "repo_revision": repo_revision,
        "ran_at": ran_at,
        "quality": quality,
        "efficiency": run.efficiency,
        "deltas": deltas,
        "controls": [c.as_dict() for c in controls],
        "warnings": warnings,
    }
    report["tradeoffs"] = _tradeoffs(report, baseline, hardware["id"])
    if not report["baseline"]:
        del report["baseline"]
    return report


def validate(report: dict) -> list[str]:
    """Schema errors for `report` (empty = valid). jsonschema is a dev dependency; the harness itself never needs it."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text())
    return sorted(e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(report))


def render_markdown(report: dict) -> str:
    c, d, eff = report["candidate"], report["dataset"], report["efficiency"]
    out = [f"# {report['kind']} evaluation: {c['name']}", "", f"**Status: {report['status'].upper()}**", ""]
    for w in report["warnings"]:
        out.append(f"> {w}")
    out += ["", f"Dataset {d['name']} {d['version']} ({d['items']} items, {round(d['reviewed_share'] * 100)}% reviewed). "
            f"Hardware {report['hardware']['id']} ({report['hardware']['cpu']}, {report['hardware']['threads']} threads). "
            f"Candidate path: {c['path']}.", "", "## Quality", "", "| metric | value | 95% CI | vs baseline | verdict |", "|---|---|---|---|---|"]
    for name, q in report["quality"].items():
        delta = report["deltas"].get(name)
        cell = f"{delta['delta']:+.4f}" if delta else "-"
        verdict = delta["verdict"] + (f" (needs ~{delta['needed_n']} items)" if delta and delta.get("needed_n") else "") if delta else "-"
        out.append(f"| {name} | {q['value']:.4f} | [{q['ci95'][0]:.4f}, {q['ci95'][1]:.4f}] | {cell} | {verdict} |")
    out += ["", "## Efficiency", "", f"- query p50 {eff['query_ms']['p50']:.1f} ms, p95 {eff['query_ms']['p95']:.1f} ms"]
    if eff.get("ms_per_chunk") is not None:
        out.append(f"- {eff['ms_per_chunk']:.1f} ms per chunk at the app's ingest batch size")
    if eff.get("rss_mb") is not None:
        out.append(f"- resident memory +{eff['rss_mb']:.0f} MB after loading")
    if eff.get("run_seconds") is not None:
        out.append(f"- whole run {eff['run_seconds']:.0f} s")
    out += ["", "## Controls", ""] + [f"- {'ok' if x['ok'] else 'FAILED'}: {x['name']} expected {x['expect']}, observed {x['observed']}" for x in report["controls"]]
    out += ["", "## Trade-offs", ""] + [f"- {t}" for t in report["tradeoffs"]]
    return "\n".join(out) + "\n"
