"""Golden-dataset evaluation harness.

Unlike tests/ (which run the graph against a fake LLM so they're fast,
hermetic, and deterministic — see tests/test_graph_integration.py), this
runs a fixed set of representative inputs through the *real* graph: real
model, real Qdrant retrieval, real tool execution. That's the whole point
— unit tests catch routing/logic regressions, this catches *behavior*
regressions (a prompt tweak, model swap, or retrieval change that quietly
makes the agent worse) that a fake LLM can't reveal, because a fake LLM's
responses are hand-scripted rather than actually reasoned.

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
class CaseResult:
    id: str
    input: str
    passed: bool
    checks: list[tuple[str, bool, str]]
    latency_s: float
    iterations: int
    tool_calls: list[str]
    answer: str


def run_case(graph, case: GoldenCase) -> CaseResult:
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

    return CaseResult(
        id=case.id,
        input=case.input,
        passed=all(ok for _, ok, _ in checks),
        checks=checks,
        latency_s=latency,
        iterations=result.get("iterations", 0),
        tool_calls=tool_calls,
        answer=answer,
    )


def print_report(results: list[CaseResult]) -> None:
    total = len(results)
    passed = sum(r.passed for r in results)
    print(f"\n{'=' * 70}")
    print(f"Golden dataset evaluation: {passed}/{total} passed ({passed / total:.0%})")
    print("=" * 70)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"\n[{status}] {r.id}  {r.input!r}")
        print(f"  answer: {r.answer[:100]!r}")
        print(
            f"  iterations={r.iterations}  tools={r.tool_calls or 'none'}  "
            f"latency={r.latency_s:.2f}s"
        )
        for name, ok, detail in r.checks:
            mark = "OK " if ok else "!! "
            print(f"    {mark}{name}: {detail}")
    avg_latency = sum(r.latency_s for r in results) / total if total else 0.0
    print(f"\nAverage latency: {avg_latency:.2f}s")


def _to_jsonable(results: list[CaseResult]) -> list[dict]:
    return [
        {
            "id": r.id,
            "passed": r.passed,
            "latency_s": r.latency_s,
            "iterations": r.iterations,
            "tool_calls": r.tool_calls,
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
    args = parser.parse_args()

    previous = None if args.no_compare else load_previous_run()

    graph = build_graph()  # real LLM, real Qdrant/embeddings — needs `make up` + `make ingest`
    results = [run_case(graph, case) for case in GOLDEN_CASES]

    print_report(results)
    if previous is not None:
        print_comparison(results, previous)
    elif not args.no_compare:
        print("\n(no previous run found — this run becomes the baseline)")

    if not args.no_save:
        path = save_run(results)
        print(f"\nSaved run to {path}")

    sys.exit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
