"""Golden-dataset evaluation harness.

Unlike tests/ (which run the graph against a fake LLM so they're fast,
hermetic, and deterministic — see tests/test_graph_integration.py), this
runs a fixed set of representative inputs through the *real* graph: real
model, real Qdrant retrieval, real tool execution. That's the whole point
— unit tests catch routing/logic regressions, this catches *behavior*
regressions (a prompt tweak, model swap, or retrieval change that quietly
makes the agent worse) that a fake LLM can't reveal, because a fake LLM's
responses are hand-scripted rather than actually reasoned.

## Statistical rigor (GRAPH_PATTERNS.md pattern 40)

Two release-gating checks, not one pass/fail per case:

- **Per-case pass RATE, not a single pass/fail.** Each case runs
  `EVAL_REPETITIONS` times (5, by default); a case is only counted as
  passing if at least `REPETITION_PASS_THRESHOLD` (80%, i.e. 4-of-5) of
  those repetitions individually passed. A stochastic system graded once
  yields a coin flip, and a gate that flips is a gate that gets disabled.
- **A grounded-claims gate over the WHOLE golden set**, using the
  runtime's own computed grounding (`used_citations` vs.
  `ungrounded_claims_count` — GRAPH_PATTERNS.md pattern 39), never a
  model's opinion of itself. `≥ GROUNDED_CLAIMS_THRESHOLD` (95%) of every
  citation marker referenced across every repetition must correspond to a
  REAL citation, or the whole run fails release-gating — this is an
  ANSWER-quality gate, distinct from (and in addition to) each case's own
  keyword/tool-usage checks, which only gate retrieval/routing.

`--repetitions N` overrides `EVAL_REPETITIONS` for fast local iteration
(`make eval -- --repetitions 1`) — the default of 5 is what CI/release
gating should actually run, but 5x the real-model latency of every case
is a real cost worth being able to opt out of during day-to-day dev.

Needs the full local stack up first: `make up`, `make pull-models`,
`make ingest`.

Run with: `make eval`
Compare against the previous run: `make eval` again (comparison is automatic
whenever eval_runs/latest.json exists from a prior run); pass --no-compare
or --no-save to opt out.
"""
import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from app.config import DEFAULT_TENANT
from app.graph import build_graph
from app.security import SecurityCtx

RESULTS_DIR = Path(__file__).resolve().parent.parent / "eval_runs"

EVAL_REPETITIONS = 5  # AR-021a: N repetitions, not a single pass/fail
REPETITION_PASS_THRESHOLD = 0.80  # 4-of-5
GROUNDED_CLAIMS_THRESHOLD = 0.95  # answer-quality gate, over the whole golden set

# Every golden case needs a valid ctx to even reach retrieve_context (see
# graph.py's route_after_validation) — DEFAULT_TENANT so search_docs can
# actually find the docs `make ingest` seeded under it.
_EVAL_CTX: SecurityCtx = {"tenant": DEFAULT_TENANT, "principal": "eval-harness", "claims": {}}


@dataclass
class GoldenCase:
    id: str
    input: str
    require_approval: bool = False
    auto_approve: bool = True  # only consulted when require_approval=True
    expect_reject: bool = False
    expect_tool: str | None = None
    expect_keywords: list[str] = field(default_factory=list)  # any one matching is a pass
    min_answer_length: int = 0


GOLDEN_CASES: list[GoldenCase] = [
    GoldenCase(
        id="empty_input_rejected",
        input="",
        expect_reject=True,
    ),
    GoldenCase(
        id="retrieval_langgraph_checkpointer",
        input="What is a LangGraph checkpointer?",
        expect_tool="search_docs",
        expect_keywords=["checkpointer", "thread_id", "memory", "persist"],
        min_answer_length=20,
    ),
    GoldenCase(
        id="retrieval_company_topic_filter",
        input="What are Acme Corp support hours?",
        expect_tool="search_docs",
        expect_keywords=["9am", "9 am", "9:00", "weekday", "5pm", "5 pm"],
        min_answer_length=10,
    ),
    GoldenCase(
        id="calculator_basic",
        input="what is 21 * 2?",
        expect_tool="calculator",
        expect_keywords=["42"],
    ),
    GoldenCase(
        id="calculator_with_human_approval",
        input="what is 12 * 7?",
        require_approval=True,
        auto_approve=True,
        expect_tool="calculator",
        expect_keywords=["84"],
    ),
    GoldenCase(
        id="general_knowledge_no_tool_needed",
        input="In one sentence, what is the capital of France?",
        expect_keywords=["paris"],
    ),
]


@dataclass
class _Attempt:
    """One repetition's raw outcome — never printed/persisted directly,
    only aggregated into a CaseResult (see run_case)."""

    passed: bool
    checks: list[tuple[str, bool, str]]
    latency_s: float
    iterations: int
    tool_calls: list[str]
    answer: str
    total_tokens: int
    total_cost_usd: float
    used_citations_count: int
    ungrounded_claims_count: int


@dataclass
class CaseResult:
    id: str
    input: str
    pass_rate: float  # fraction of repetitions that individually passed
    repetitions: int
    passed: bool  # pass_rate >= REPETITION_PASS_THRESHOLD
    checks: list[tuple[str, bool, str]]  # from the LAST repetition, for the printed detail
    latency_s: float  # averaged across repetitions
    iterations: int  # from the last repetition
    tool_calls: list[str]  # from the last repetition
    answer: str  # from the last repetition
    total_tokens: int  # SUMMED across repetitions
    total_cost_usd: float  # SUMMED across repetitions
    used_citations_count: int  # SUMMED across repetitions
    ungrounded_claims_count: int  # SUMMED across repetitions


def _run_case_once(graph, case: GoldenCase) -> _Attempt:
    config = {
        "configurable": {
            "thread_id": f"eval-{case.id}-{uuid.uuid4()}",
            "ctx": _EVAL_CTX,
        }
    }
    start = time.monotonic()

    result = graph.invoke(
        {
            "messages": [HumanMessage(content=case.input)],
            "require_approval": case.require_approval,
        },
        config=config,
    )
    state = graph.get_state(config)
    while state.next:  # paused at a human_approval interrupt
        result = graph.invoke(Command(resume=case.auto_approve), config=config)
        state = graph.get_state(config)

    latency = time.monotonic() - start
    answer = result["messages"][-1].content
    tool_calls = sorted(
        {
            tc["name"]
            for m in result["messages"]
            if isinstance(m, AIMessage)
            for tc in (m.tool_calls or [])
        }
    )

    checks: list[tuple[str, bool, str]] = []
    if case.expect_reject:
        checks.append(("rejected", "try again" in answer.lower(), answer[:80]))
    else:
        checks.append(
            ("not_rejected", "try again" not in answer.lower(), answer[:80])
        )
        if case.expect_tool:
            checks.append(
                (
                    f"used_tool:{case.expect_tool}",
                    case.expect_tool in tool_calls,
                    f"tools used: {tool_calls or 'none'}",
                )
            )
        if case.expect_keywords:
            found = any(kw.lower() in answer.lower() for kw in case.expect_keywords)
            checks.append(
                (
                    "answer_keywords",
                    found,
                    f"expected one of {case.expect_keywords!r} in answer",
                )
            )
        if case.min_answer_length:
            checks.append(
                (
                    "min_answer_length",
                    len(answer) >= case.min_answer_length,
                    f"len={len(answer)}, want >= {case.min_answer_length}",
                )
            )

    return _Attempt(
        passed=all(ok for _, ok, _ in checks),
        checks=checks,
        latency_s=latency,
        iterations=result.get("iterations", 0),
        tool_calls=tool_calls,
        answer=answer,
        total_tokens=result.get("total_tokens", 0),
        total_cost_usd=result.get("total_cost_usd", 0.0),
        used_citations_count=len(result.get("used_citations") or []),
        ungrounded_claims_count=result.get("ungrounded_claims_count", 0),
    )


def run_case(graph, case: GoldenCase, repetitions: int = EVAL_REPETITIONS) -> CaseResult:
    """Runs `case` `repetitions` times and aggregates — see module
    docstring for why this is a rate, not a single pass/fail."""
    attempts = [_run_case_once(graph, case) for _ in range(repetitions)]
    last = attempts[-1]
    pass_rate = sum(a.passed for a in attempts) / len(attempts)
    return CaseResult(
        id=case.id,
        input=case.input,
        pass_rate=pass_rate,
        repetitions=repetitions,
        passed=pass_rate >= REPETITION_PASS_THRESHOLD,
        checks=last.checks,
        latency_s=sum(a.latency_s for a in attempts) / len(attempts),
        iterations=last.iterations,
        tool_calls=last.tool_calls,
        answer=last.answer,
        total_tokens=sum(a.total_tokens for a in attempts),
        total_cost_usd=sum(a.total_cost_usd for a in attempts),
        used_citations_count=sum(a.used_citations_count for a in attempts),
        ungrounded_claims_count=sum(a.ungrounded_claims_count for a in attempts),
    )


def grounded_claims_ratio(results: list[CaseResult]) -> float:
    """AR-021a's answer-quality gate: of every citation marker referenced
    across every case and every repetition, what fraction were REAL
    (matched a real citation) vs. invented. `1.0` (vacuously grounded)
    when nothing ever cited anything at all — a golden set with no
    citing cases isn't a grounding FAILURE, it's a set that doesn't
    exercise this gate; the per-case checks still cover it independently.
    """
    total_used = sum(r.used_citations_count for r in results)
    total_ungrounded = sum(r.ungrounded_claims_count for r in results)
    total = total_used + total_ungrounded
    return 1.0 if total == 0 else total_used / total


def print_report(results: list[CaseResult]) -> None:
    total = len(results)
    passed = sum(r.passed for r in results)
    print(f"\n{'=' * 70}")
    print(f"Golden dataset evaluation: {passed}/{total} cases passed ({passed / total:.0%})")
    print("=" * 70)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"\n[{status}] {r.id}  {r.input!r}")
        print(f"  pass_rate: {r.pass_rate:.0%} ({r.repetitions} repetitions)")
        print(f"  answer (last repetition): {r.answer[:100]!r}")
        print(
            f"  iterations={r.iterations}  tools={r.tool_calls or 'none'}  "
            f"avg_latency={r.latency_s:.2f}s"
        )
        print(
            f"  tokens(sum)={r.total_tokens}  cost(sum)=${r.total_cost_usd:.4f}  "
            f"grounded={r.used_citations_count}  ungrounded={r.ungrounded_claims_count}"
        )
        for name, ok, detail in r.checks:
            mark = "OK " if ok else "!! "
            print(f"    {mark}{name}: {detail}")

    avg_latency = sum(r.latency_s for r in results) / total if total else 0.0
    ratio = grounded_claims_ratio(results)
    ratio_ok = ratio >= GROUNDED_CLAIMS_THRESHOLD
    print(f"\nAverage latency: {avg_latency:.2f}s")
    print(
        f"Grounded-claims ratio: {ratio:.1%} "
        f"({'OK' if ratio_ok else 'FAIL'}, threshold {GROUNDED_CLAIMS_THRESHOLD:.0%})"
    )


def _to_jsonable(results: list[CaseResult]) -> list[dict]:
    return [
        {
            "id": r.id,
            "passed": r.passed,
            "pass_rate": r.pass_rate,
            "repetitions": r.repetitions,
            "latency_s": r.latency_s,
            "iterations": r.iterations,
            "tool_calls": r.tool_calls,
            "total_tokens": r.total_tokens,
            "total_cost_usd": r.total_cost_usd,
            "used_citations_count": r.used_citations_count,
            "ungrounded_claims_count": r.ungrounded_claims_count,
            "checks": [{"name": n, "passed": ok, "detail": d} for n, ok, d in r.checks],
        }
        for r in results
    ]


def load_previous_run() -> dict[str, dict] | None:
    latest = RESULTS_DIR / "latest.json"
    if not latest.exists():
        return None
    return {c["id"]: c for c in json.loads(latest.read_text())}


def save_run(results: list[CaseResult]) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = json.dumps(_to_jsonable(results), indent=2)
    (RESULTS_DIR / f"{ts}.json").write_text(payload)
    (RESULTS_DIR / "latest.json").write_text(payload)
    return RESULTS_DIR / f"{ts}.json"


def print_comparison(results: list[CaseResult], previous: dict[str, dict]) -> None:
    print(f"\n{'-' * 70}")
    print("Comparison vs. previous run")
    print("-" * 70)
    print(f"{'case':<38}{'before':<10}{'after':<10}")
    for r in results:
        prev = previous.get(r.id)
        before = "n/a" if prev is None else ("PASS" if prev["passed"] else "FAIL")
        after = "PASS" if r.passed else "FAIL"
        flag = "  <-- regression" if before == "PASS" and after == "FAIL" else ""
        flag = flag or ("  (new pass)" if before == "FAIL" and after == "PASS" else "")
        print(f"{r.id:<38}{before:<10}{after:<10}{flag}")

    prev_latencies = [p["latency_s"] for p in previous.values()]
    curr_latencies = [r.latency_s for r in results]
    if prev_latencies and curr_latencies:
        before_avg = sum(prev_latencies) / len(prev_latencies)
        after_avg = sum(curr_latencies) / len(curr_latencies)
        print(f"\navg latency: {before_avg:.2f}s -> {after_avg:.2f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-compare", action="store_true", help="skip before/after comparison"
    )
    parser.add_argument(
        "--no-save", action="store_true", help="don't persist this run for future comparisons"
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=EVAL_REPETITIONS,
        help=f"repetitions per case (default {EVAL_REPETITIONS}, per AR-021a; "
        "lower for faster local iteration, e.g. --repetitions 1)",
    )
    args = parser.parse_args()

    previous = None if args.no_compare else load_previous_run()

    graph = build_graph()  # real LLM, real Qdrant/embeddings — needs `make up` + `make ingest`
    results = [run_case(graph, case, repetitions=args.repetitions) for case in GOLDEN_CASES]

    print_report(results)
    if previous is not None:
        print_comparison(results, previous)
    elif not args.no_compare:
        print("\n(no previous run found — this run becomes the baseline)")

    if not args.no_save:
        path = save_run(results)
        print(f"\nSaved run to {path}")

    cases_ok = all(r.passed for r in results)
    grounding_ok = grounded_claims_ratio(results) >= GROUNDED_CLAIMS_THRESHOLD
    sys.exit(0 if cases_ok and grounding_ok else 1)


if __name__ == "__main__":
    main()
