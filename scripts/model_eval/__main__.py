"""`python -m scripts.model_eval <command>` (specs/014 contracts/model-eval.md). A development tool, never part of a service image.

Exit codes: 0 ok | 1 unexpected error | 2 usage or configuration error | 3 a prerequisite is missing (a model, dataset or service): NO
report is written | 4 verification failed (a hash, a split, a licence, a baseline from another dataset) | 5 the run completed but a
control gave the wrong verdict: the report is written, marked INVALID, and must not be used.
"""
import argparse
import dataclasses
import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from scripts.model_eval import baselines, hardware, head, route_label, routing
from scripts.model_eval import datasets as ds
from scripts.model_eval.candidates import Candidate, load_candidates
from scripts.model_eval.controls import CONTROL_CANDIDATES, derived_controls
from scripts.model_eval.report import RunOutput, build_report, render_markdown, validate
from scripts.model_eval.runners import classify, embedding, rerank, screen
from scripts.model_eval.runners.base import (
    PrerequisiteMissing,
    RunSettings,
    latest_version,
)
from scripts.model_eval.stats import measure
from scripts.model_eval.suites import suite_for

OK, ERROR, USAGE, MISSING, VERIFY_FAILED, CONTROL_FAILED = 0, 1, 2, 3, 4, 5
REPO = Path(__file__).resolve().parents[2]
EVALS = REPO / "evals"
CANDIDATES_DIR = EVALS / "candidates"
BASELINES_DIR = EVALS / "baselines"
REPORTS_DIR = REPO / "var" / "model_eval" / "reports"
# kind -> the module whose `run(candidate, settings, via=...)` measures it. A dict so tests can swap in fakes.
RUNNERS: dict[str, ModuleType] = {"embedding": embedding, "reranker": rerank, "screen": screen, "classifier": classify}
CANDIDATE_FILES = {"embedding": "embedding.toml", "reranker": "reranker.toml", "screen": "screen.toml", "classifier": "classifier.toml"}


class UsageError(Exception):
    pass


def _repo_revision() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def declared(kind: str, candidates_dir: Path | None = None) -> list[Candidate]:
    # The directory is looked up when called, not bound as a default, so tests can point it at a temporary one.
    return load_candidates((candidates_dir or CANDIDATES_DIR) / CANDIDATE_FILES[kind], expected_kind=kind)


def baseline_candidate(kind: str, candidates_dir: Path | None = None) -> Candidate:
    marked = [c for c in declared(kind, candidates_dir) if c.baseline]
    if len(marked) != 1:
        raise UsageError(f"{CANDIDATE_FILES[kind]} must mark exactly one candidate `baseline = true` (found {len(marked)})")
    return marked[0]


def resolve_candidate(kind: str, name: str, candidates_dir: Path | None = None) -> Candidate:
    """A declared candidate, `baseline-as-candidate` (the baseline model run again under another name), or a named degenerate control."""
    if name == "baseline-as-candidate":
        return dataclasses.replace(baseline_candidate(kind, candidates_dir), name=name, baseline=False)
    if name in CONTROL_CANDIDATES.get(kind, {}):
        return Candidate(name=name, kind=kind, path="inprocess", licence="n/a (a control, no model)")
    found = next((c for c in declared(kind, candidates_dir) if c.name == name), None)
    if found is None:
        known = [c.name for c in declared(kind, candidates_dir)] + list(CONTROL_CANDIDATES.get(kind, {}))
        raise UsageError(f"no {kind} candidate named {name!r}; declared: {sorted(known)}")
    return found


def _settings(args: argparse.Namespace) -> RunSettings:
    if args.splits != "test":
        raise UsageError("only the test split is reported; dev is for tuning and is refused for reporting (FR-010)")
    extra = {"head_dir": Path(args.head_dir)} if args.head_dir else {}
    return RunSettings(split="test", runs=args.runs, seed=args.seed, threads=args.threads, dataset=args.dataset, list_size=args.list_size,
                       outcomes=Path(args.outcomes) if args.outcomes else None, margin=args.margin, **extra)


def _execute(args: argparse.Namespace) -> tuple[Candidate, RunOutput]:
    if args.kind not in RUNNERS:
        raise UsageError(f"kind {args.kind!r} has no runner in this build")
    candidate = resolve_candidate(args.kind, args.candidate)
    load_at_start = hardware.load_snapshot()  # before the run: the run's own load must not count as "the machine was busy"
    output = RUNNERS[args.kind].run(candidate, _settings(args), via=args.via)
    busy = hardware.load_warning(at_start=load_at_start)
    if busy:
        output.warnings.append(busy)
    return candidate, output


def _write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    stem = f"{report['kind']}-{report['candidate']['name']}-{report['dataset']['name']}-{stamp}"
    (out_dir / f"{stem}.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    (out_dir / f"{stem}.md").write_text(render_markdown(report))
    return out_dir / f"{stem}.json"


def cmd_run_all(args: argparse.Namespace) -> int:
    """`run` for every kind that has this candidate (a declared one or a named control). The worst exit code wins, so one invalid
    report is not hidden behind later successes."""
    worst = OK
    ran = 0
    for kind in RUNNERS:
        try:
            resolve_candidate(kind, args.candidate)
        except UsageError:
            continue  # this candidate does not exist for this kind
        ran += 1
        sys.stdout.write(f"\n=== {kind} ===\n")
        worst = max(worst, cmd_run(argparse.Namespace(**{**vars(args), "kind": kind})))
    if not ran:
        raise UsageError(f"no kind declares a candidate named {args.candidate!r}")
    return worst


def cmd_run(args: argparse.Namespace) -> int:
    if args.all_kinds:
        args.all_kinds = False
        return cmd_run_all(args)
    if not args.kind:
        raise UsageError("--kind is required (or --all-kinds)")
    candidate, output = _execute(args)
    suite = suite_for(args.kind, dataset=output.dataset["name"])
    baseline = None
    if not args.no_compare:
        base_candidate = baseline_candidate(args.kind)
        baseline = baselines.find_baseline(BASELINES_DIR, args.kind, base_candidate.name, output.dataset["name"], output.dataset["version"])
        if baseline is None:
            output.warnings.append(f"no baseline recorded for {base_candidate.name} on {output.dataset['name']} {output.dataset['version']}; nothing to compare against")
    hw = hardware.profile(args.threads)
    report = build_report(output, suite, baseline, hardware=hw, repo_revision=_repo_revision(), ran_at=datetime.now(UTC).isoformat(timespec="seconds"))
    problems = validate(report)
    if problems:  # a malformed report is a bug in the harness, not something to hand a reader
        raise RuntimeError("the report does not match its schema: " + "; ".join(problems))
    path = _write_report(report, Path(args.out))
    sys.stdout.write(render_markdown(report))
    sys.stdout.write(f"\nreport: {path}\n")
    return CONTROL_FAILED if report["status"] == "invalid" else OK


def cmd_baseline(args: argparse.Namespace) -> int:
    if not args.kind:
        raise UsageError("--kind is required")
    candidate, output = _execute(args)
    if output.dataset["reviewed_share"] < 1.0:
        sys.stdout.write("PROVISIONAL: part of the evaluation set is not yet reviewed by a person; this baseline is marked provisional.\n")
    suite = suite_for(args.kind, dataset=output.dataset["name"])
    hw = hardware.profile(args.threads)
    existing = baselines.path_for(BASELINES_DIR, args.kind, candidate.name, output.dataset["name"], output.dataset["version"])
    if args.add_hardware:
        if not existing.exists():
            raise UsageError(f"--add-hardware needs an existing baseline at {existing}")
        baselines.merge_efficiency(existing, hw["id"], output.efficiency)
        sys.stdout.write(f"added the latency of {hw['id']} to {existing}\n")
        return OK
    manifest_sha = None
    manifest_path = ds.DATASETS_ROOT / output.dataset["name"]
    versions = sorted(manifest_path.glob("v*/manifest.json")) if manifest_path.exists() else []
    if versions:
        manifest_sha = ds.sha256_file(versions[-1])
    record = {
        "kind": args.kind,
        "candidate": candidate.name,
        "dataset": {**{k: output.dataset[k] for k in ("name", "version", "reviewed_share")}, "manifest_sha256": manifest_sha},
        "model": {k: output.candidate.get(k) for k in ("identity", "revision", "licence", "size_bytes", "dim")},
        "path": output.candidate["path"],
        "repo_revision": _repo_revision(),
        "quality": {k: {"mean": v.value, "ci": list(v.ci95)} for k, v in measure(suite, output.data).items()},
        "efficiency": {hw["id"]: output.efficiency},
        "hardware": {k: hw[k] for k in ("cpu", "cores", "threads", "onnxruntime", "os")},
        "warnings": output.warnings,  # kept with the record: a latency taken on a busy machine must not later look authoritative
        "recorded_at": datetime.now(UTC).date().isoformat(),
    }
    path = baselines.write_baseline(BASELINES_DIR, record, output.data, force=args.force)
    sys.stdout.write(f"baseline recorded: {path}\n")
    return OK


def cmd_verify(args: argparse.Namespace) -> int:
    """No model, no network: every committed dataset and baseline is intact, licences are allowed, splits are disjoint, and the derived
    controls behave on each baseline's data."""
    problems: list[str] = []
    seen = 0
    for manifest in sorted(ds.DATASETS_ROOT.glob("*/v*/manifest.json")):
        seen += 1
        problems += ds.verify_dataset(manifest.parent)
    try:
        ds.load_public_lock()
    except ds.DatasetError as exc:
        problems.append(str(exc))
    for kind in CANDIDATE_FILES:
        try:
            declared(kind)
        except ValueError as exc:
            problems.append(f"{CANDIDATE_FILES[kind]}: {exc}")
    for path in sorted(BASELINES_DIR.glob("*/*.json")):
        if path.name.endswith(".items.json"):
            continue
        try:
            record = json.loads(path.read_text())
            items = path.parent / record["per_query"]
            import hashlib

            if hashlib.sha256(items.read_bytes()).hexdigest() != record.get("per_query_sha256"):
                problems.append(f"{path.name}: the per-item file does not match its recorded hash")
                continue
            baseline = baselines.read_baseline(path)
            suite = suite_for(record["kind"], dataset=record["dataset"]["name"])
            for control in derived_controls(record["kind"], suite, baseline.data):
                if not control.ok:
                    problems.append(f"{path.name}: control {control.name} expected {control.expect}, observed {control.observed}")
        except (OSError, KeyError, ValueError, baselines.BaselineMismatch) as exc:
            problems.append(f"{path.name}: {type(exc).__name__}: {exc}")
    if problems:
        sys.stdout.write("verification FAILED:\n  " + "\n  ".join(problems) + "\n")
        return VERIFY_FAILED
    sys.stdout.write(f"verified {seen} dataset(s), the public lock, the candidate declarations and the recorded baselines\n")
    return OK


def cmd_fetch(args: argparse.Namespace) -> int:
    entries = ds.load_public_lock()
    if not entries:
        sys.stdout.write("no public datasets are pinned\n")
        return OK
    for entry in entries:
        ds.fetch_public(entry)
        sys.stdout.write(f"{entry.id}: cached and verified ({entry.licence})\n")
    return OK


def _route_prompts(manifest: ds.Manifest) -> list[dict]:
    """Every prompt of the committed route set, both splits: the labeller scores them all (dev to train a head, test to report it)."""
    return [{"id": r["id"], "text": r["text"]} for split in sorted(manifest.raw["splits"]) for r in manifest.split_items(split)]


def cmd_route_label(args: argparse.Namespace) -> int:
    """Run the committed route prompts through the local and the strong alias and have the judge alias score both answers (T071)."""
    version_dir = latest_version(ds.DATASETS_ROOT, args.dataset)
    manifest = ds.load_verified(version_dir)
    prompts = _route_prompts(manifest)[: args.limit] if args.limit else _route_prompts(manifest)
    chat = route_label.gateway_chat()
    for alias in dict.fromkeys((args.local, args.strong, args.judge)):  # a typo'd alias would otherwise cost an hour of "missing" rows
        try:
            chat(alias, [{"role": "user", "content": "Reply with the single word: ready"}], 120.0)
        except Exception as exc:  # noqa: BLE001 - whatever is wrong, say which alias and stop before spending time
            raise PrerequisiteMissing(f"the gateway did not answer for alias {alias!r} ({type(exc).__name__}); is the stack up (`make up`) and OPENAI_API_BASE correct?") from exc
    out = routing.outcomes_dir(version_dir) / f"{args.strong_id or args.strong}.jsonl"
    result = route_label.label_prompts(
        prompts, local=args.local, strong=args.strong, judge=args.judge, chat=chat, out_path=out, call_timeout=args.call_timeout, deadline=args.deadline
    )
    route_label.write_meta(
        out.with_suffix(".meta.json"), local=args.local, strong=args.strong, judge=args.judge,
        dataset={"name": manifest.name, "version": manifest.version, "prompts": len(prompts)}, run=result, finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    sys.stdout.write(f"outcomes: {out}\nscored {result.scored}, skipped {result.skipped} already done, {result.missing} with a missing score, in {result.seconds:.0f} s\n")
    if result.failures:
        sys.stdout.write(f"failures (counts only): {dict(sorted(result.failures.items()))}\n")
    if result.stopped_early or result.missing:
        sys.stdout.write("not every prompt has both scores; re-run the same command to retry the rest (finished prompts are kept)\n")
    return OK


def cmd_head_train(args: argparse.Namespace) -> int:
    """Fit a head for one classifier model from labelled outcomes and write it, unaccepted, for a person to read the report on (T070)."""
    version_dir = latest_version(ds.DATASETS_ROOT, args.dataset)
    manifest = ds.load_verified(version_dir)
    outcomes_path = classify.find_outcomes(version_dir, Path(args.outcomes) if args.outcomes else None)
    if outcomes_path is None:
        raise PrerequisiteMissing(f"no routing outcomes under {routing.outcomes_dir(version_dir)}; label the prompts first with `make router-label JUDGE=<alias>`")
    outcomes = routing.read_outcomes(outcomes_path)
    judges = sorted({o.judge for o in outcomes.values()})

    def pairs(split: str) -> list[tuple[str, routing.Outcome]]:
        return [(r["text"], outcomes[r["id"]]) for r in manifest.split_items(split) if r["id"] in outcomes]

    embed, info = head.service_embedder(args.model)
    trained = head.train_head(
        model=info, embed=embed, train=pairs("dev"), heldout=pairs("test"), margin=args.margin,
        dataset={"dataset": manifest.name, "version": manifest.version, "judge": ", ".join(judges)}, outcomes_sha256=routing.outcomes_sha256(outcomes_path),
    )
    json_path, npz_path = head.write_head(Path(args.out), trained, trained_at=datetime.now(UTC).isoformat(timespec="seconds"), force=args.force)
    held, beaten = trained.provenance["heldout"], trained.provenance["controls_beaten"]
    sys.stdout.write(
        f"wrote {json_path.name} and {npz_path.name} in {json_path.parent}\n"
        f"held-out ({held['items']} prompts): accuracy {held['accuracy']:.3f}, macro-F1 {held['macro_f1']:.3f}, ECE {held['ece']:.3f}, "
        f"average gap recovered {held['apgr'] if held['apgr'] is None else round(held['apgr'], 3)}; beats random: {beaten['random']}, beats longest-first: {beaten['length']}\n"
        "accepted: false. Run `make model-eval KIND=classifier CANDIDATE=<name> VIA=inprocess` and read the report; accepting is a person's decision "
        "(set \"accepted\": true in the JSON, mount the directory as ROUTER_HEAD_DIR, restart the ML service).\n"
    )
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.model_eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--kind", choices=sorted(CANDIDATE_FILES), default=None)
        p.add_argument("--candidate", required=True)
        p.add_argument("--via", choices=["gateway", "service", "inprocess"], default=None, help="override the candidate's declared path")
        p.add_argument("--dataset", default=None, help="a repository-own dataset or a pinned public id (default: the kind's own)")
        p.add_argument("--splits", default="test")
        p.add_argument("--runs", type=int, default=3, help="repeats of the latency loop")
        p.add_argument("--seed", type=int, default=20261017)
        p.add_argument("--threads", type=int, default=None, help="ONNX intra-op threads for in-process models")
        p.add_argument("--list-size", type=int, default=None, help="reranker candidate-list size for public sets")
        p.add_argument("--outcomes", default=None, help="classifier: the labelled outcomes file (default: the only one in the dataset's outcomes/)")
        p.add_argument("--margin", type=float, default=routing.DEFAULT_MARGIN, help="classifier: strong minus local must reach this to need the stronger model")
        p.add_argument("--head-dir", default=None, help="classifier, in-process: the directory `head train` wrote to (default: var/model_eval/heads)")

    run = sub.add_parser("run", help="evaluate one candidate and compare it with the recorded baseline")
    run.set_defaults(all_kinds=False)
    common(run)
    run.add_argument("--out", default=str(REPORTS_DIR))
    run.add_argument("--no-compare", action="store_true")
    run.add_argument("--all-kinds", action="store_true", help="run this candidate for every kind that declares it")
    run.set_defaults(func=cmd_run)

    base = sub.add_parser("baseline", help="evaluate a candidate and RECORD it as the baseline")
    common(base)
    base.add_argument("--force", action="store_true", help="overwrite an existing baseline (it is immutable otherwise)")
    base.add_argument("--add-hardware", action="store_true", help="add this machine's latency to an existing baseline instead")
    base.set_defaults(func=cmd_baseline)

    sub.add_parser("verify", help="check every committed dataset, baseline, licence and control (no model)").set_defaults(func=cmd_verify)
    sub.add_parser("fetch", help="download and verify the pinned public datasets").set_defaults(func=cmd_fetch)

    label = sub.add_parser("route-label", help="score the route prompts' answers from the local and strong model with a judge (needs the gateway)")
    label.add_argument("--judge", required=True, help="the gateway alias that scores each answer 0-1; it should not be the model being judged")
    label.add_argument("--local", default="chat", help="the gateway alias of the local model (default: chat)")
    label.add_argument("--strong", default="chat-strong", help="the gateway alias of the stronger model (default: chat-strong)")
    label.add_argument("--strong-id", default=None, help="the outcomes file's name (default: the strong alias)")
    label.add_argument("--dataset", default="route-domains")
    label.add_argument("--call-timeout", type=float, default=180.0, help="seconds allowed to each model or judge call")
    label.add_argument("--deadline", type=float, default=None, help="seconds after which no new call starts (the run keeps what it has)")
    label.add_argument("--limit", type=int, default=None, help="only the first N prompts (a smoke test); finished prompts are kept for the next run")
    label.set_defaults(func=cmd_route_label)

    heads = sub.add_parser("head", help="routing heads")
    heads_sub = heads.add_subparsers(dest="head_command", required=True)
    train = heads_sub.add_parser("train", help="fit a head from labelled outcomes and write it, never accepted")
    train.add_argument("--model", required=True, help="a classify-role model in docker/ml-service/models.toml")
    train.add_argument("--dataset", default="route-domains")
    train.add_argument("--outcomes", default=None, help="the outcomes file (default: the only one in the dataset's outcomes/ directory)")
    train.add_argument("--margin", type=float, default=routing.DEFAULT_MARGIN)
    train.add_argument("--out", default=str(REPO / "var" / "model_eval" / "heads"), help="where to write head-<model>.json and .npz")
    train.add_argument("--force", action="store_true", help="replace a head a person has already accepted")
    train.set_defaults(func=cmd_head_train)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        sys.stderr.write(f"usage error: {exc}\n")
        return USAGE
    except ValueError as exc:  # a bad candidate declaration, caught at load
        sys.stderr.write(f"configuration error: {exc}\n")
        return USAGE
    except head.ContaminatedTrainingSet as exc:
        sys.stderr.write(f"verification failed: {exc}\n")
        return VERIFY_FAILED
    except (head.TrainingRefused, routing.OutcomesError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return USAGE
    except baselines.BaselineExists as exc:
        sys.stderr.write(f"{exc}\n")
        return USAGE
    except PrerequisiteMissing as exc:
        sys.stderr.write(f"prerequisite missing (no report written): {exc}\n")
        return MISSING
    except (ds.DatasetError, baselines.BaselineMismatch) as exc:
        sys.stderr.write(f"verification failed: {exc}\n")
        return VERIFY_FAILED
    except Exception as exc:  # noqa: BLE001 - last line of defence: say what broke, exit 1, never a bare traceback for an operator
        sys.stderr.write(f"unexpected error: {type(exc).__name__}: {exc}\n")
        return ERROR


if __name__ == "__main__":
    sys.exit(main())
