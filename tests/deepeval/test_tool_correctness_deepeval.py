"""Tool-call TRAJECTORY correctness against the real graph, via deepeval (`@pytest.mark.deepeval`, `make
deepeval`; GRAPH_PATTERNS.md pattern 48).

A different question from every other check: `scripts/eval.py`'s `GoldenCase.expect_tool` checks only that a
tool was called, never its arguments, and the RAG-quality and conversation files judge the FINAL ANSWER.
Nothing else asks whether the agent called the right tool, and called it well.

Two metrics with different reliability (checked in deepeval's installed source):
- `ToolCorrectnessMetric`'s score is a DETERMINISTIC name/type match between `tools_called` and
  `expected_tools` (`model=`/`include_reason=True` only writes the explanation), as reliable as `expect_tool`
  but a real set comparison.
- `ArgumentCorrectnessMetric` IS LLM-judged (per-claim verdicts like `FaithfulnessMetric`): does the generated
  search QUERY make sense for the question? It is the metric that needed the stronger judge (`deepeval_judge`,
  Gemini 3.1 Flash-Lite, the model and key `promptfoo/redteam.yaml` uses); on the small local judge such
  questions gave self-contradictory scores (pattern 48).

Six cases in two groups:
- `search_docs`/`calculator`/`query_employees`: the tools this domain's small-model routing has been confused
  about. `search_docs` reuses the "Ecorp support hours" wording of `test_rag_quality_deepeval.py`, which
  replaced `scripts/eval.py`'s `retrieval_company_topic_filter` and `calculator_basic` cases (retired
  2026-09-16). `calculator` and `query_employees` guard the other direction: did tightening the prompt for the
  search_docs collision make them too narrow to fire? That overcorrection happened once (calculator briefly
  stopped firing on real math after an early SYSTEM_PROMPT edit).
- `skill_search`/`use_skill`, `run_subagent`, `fetch_external_reference` (added 2026-09-16): skills and
  subagents had no live coverage, only hermetic fake-LLM tests, and crawl4ai's live test uses a scripted LLM,
  proving the mechanics but not whether a REAL model reaches for it.

History worth keeping: this file surfaced a word collision between "support hours" and the
`Department.support` enum value (the model called `query_employees` instead of `search_docs`), and that three
prompt edits to fix it had no effect because the agent ran with NO system prompt (a directly built graph was
never seeded). The agent node now puts the domain's prompt first on every call, so a directly built graph is
never un-guided (this suite ran that way for weeks unnoticed).
"""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.ingestion import web_crawler
from tests.conftest import TEST_CTX
from tests.ecorp import build_graph
from tests.ollama import ollama_deps

pytestmark = pytest.mark.deepeval

_CONTEXT = (
    "[1] Ecorp support hours are 9am to 5pm Monday through Friday, "
    "closed on weekends and public holidays."
)
_CITATIONS = [
    {
        "marker": "[1]",
        "doc_id": "support-hours-doc",
        "title": "support-hours",
        "text": "Ecorp support hours are 9am to 5pm Monday through Friday.",
        "score": 0.9,
    },
]


async def _real_search(query: str, ctx=None) -> tuple[str, list[dict]]:
    return _CONTEXT, _CITATIONS


async def _run_and_get_tool_calls(endpoint: dict, question: str, manifest=None, domain=None, auto_approve: bool = False):
    """Shared by every test below: build a fresh graph/thread, run one
    turn, and extract (answer, tools_called) in the shape `ToolCall` needs.
    Same extraction shape scripts/eval.py's own `tool_calls` field already
    uses (`tc["name"] for m in result["messages"] if isinstance(m,
    AIMessage) for tc in (m.tool_calls or [])`), plus `tc["args"]`, which
    that file's own binary presence check never needed.

    `manifest`/`domain` default to the unscoped Ecorp domain, matching
    every test in this file except the crawl4ai one below — support's
    `fetch_external_reference` (and ops's `check_vendor_status_page`)
    don't exist on the default domain's toolset at all (confirmed against
    app/domains/support/tools.py's own TOOLS list), so that test needs a
    real `manifest`/`domain` override.

    `auto_approve` handles the SAME human_approval interrupt
    `test_conversation_simulator_deepeval.py`'s `model_callback` and
    `test_domain_crawl_tools_live.py` already handle — every "outward"
    tool (fetch_external_reference included) always pauses for approval
    first; a case that never calls one of those tools just never hits the
    pause, so leaving this off by default costs nothing for the other
    tests here.
    """
    from deepeval.test_case import ToolCall
    from langgraph.types import Command

    graph = build_graph(ollama_deps(endpoint, search_docs=_real_search), manifest=manifest, domain=domain)
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=question)]},
        config=config,
    )
    if auto_approve:
        state = await graph.aget_state(config)
        if state.next:  # paused at human_approval, not finished
            result = await graph.ainvoke(Command(resume=True), config=config)
    answer = result["messages"][-1].content
    tools_called = [
        ToolCall(name=tc["name"], input_parameters=tc.get("args") or {})
        for m in result["messages"]
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
    ]
    return answer, tools_called


async def test_search_docs_tool_call_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """The original case this file was built around — see its own module
    docstring for the real, disclosed word-collision finding this
    surfaced (fixed in Ecorp's system prompt and the tools' docstrings) and the
    missing-system-prompt finding it then went on to surface (fixed structurally: see the
    module docstring)."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = "What are Ecorp's support hours?"
    answer, tools_called = await _run_and_get_tool_calls(deepeval_ollama, question)

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        tools_called=tools_called,
        expected_tools=[ToolCall(name="search_docs")],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            ToolCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
            ArgumentCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


async def test_calculator_tool_call_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """Regression coverage for the SAME prompt fix above, from the other
    direction: does a genuine arithmetic question still correctly reach
    `calculator`, with the right expression, now that the fix explicitly
    narrows calculator's own instruction to "a literal arithmetic
    expression the user actually wrote out"? A prompt fix aimed at one
    failure mode (search_docs vs. calculator/query_employees confusion)
    can always overcorrect into a new one in the opposite direction — this
    is the check that would catch calculator being scoped too narrowly to
    fire on real math anymore, the same class of regression this whole
    investigation's own SYSTEM_PROMPT edits produced once already (see
    app/agent/graph/core.py's own history/GRAPH_PATTERNS.md pattern 48)."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = "What is 12 times 7?"
    answer, tools_called = await _run_and_get_tool_calls(deepeval_ollama, question)

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        tools_called=tools_called,
        expected_tools=[ToolCall(name="calculator")],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            ToolCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
            ArgumentCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


async def test_query_employees_tool_call_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """Regression coverage from the THIRD direction: a genuine staff-
    lookup question should still reach `query_employees`, now that
    search_docs/tools.py's own docstring explicitly claims "support hours"-
    style company-facts questions away from it. Deliberately asks about
    the Sales department, not Support — this checks the tool still works
    at all post-fix, not a re-run of the original word-collision case
    (that's `test_search_docs_tool_call_is_correct_and_well_argued`
    above)."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = "Who works in the Sales department at Ecorp?"
    answer, tools_called = await _run_and_get_tool_calls(deepeval_ollama, question)

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        tools_called=tools_called,
        expected_tools=[ToolCall(name="query_employees")],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            ToolCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
            ArgumentCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


async def test_skill_tool_calls_are_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """A genuinely different mechanism from the three tool-selection cases
    above: not "which of several DATA tools fits this question," but "does
    the model recognize a packaged, multi-step INSTRUCTION set exists for
    this task and route to it first," exactly as SYSTEM_PROMPT instructs
    ("For a task that might have packaged, multi-step instructions... call
    skill_search first; if it returns a good match, call use_skill with
    that exact name"). Zero live/semantic coverage of this existed before
    today — tests/agent/tools/test_skills.py is hermetic (fake LLM), and nothing
    else in tests/live/ touches skills at all.

    `onboarding-brief` (skills/onboarding-brief/SKILL.md) is the right
    fixture for this: it's one of the few bundled skills with NO `domains:`
    frontmatter restriction (confirmed by reading the file directly, not
    assumed from its name), so it's actually reachable from the same
    unscoped Ecorp domain every other test in this file already uses — no
    new manifest/domain wiring needed."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = "Can you put together an onboarding brief for a new hire named Alex Rivera?"
    answer, tools_called = await _run_and_get_tool_calls(deepeval_ollama, question)

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        tools_called=tools_called,
        expected_tools=[ToolCall(name="skill_search"), ToolCall(name="use_skill")],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            ToolCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
            ArgumentCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


async def test_subagent_delegation_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """Does the model delegate a self-contained lookup to `run_subagent` when a bundled subagent's description
    fits, and, since ArgumentCorrectnessMetric applies here too, hand it a CLEAR, COMPLETE task description?
    That matters most here: SYSTEM_PROMPT warns a subagent "has no access to this conversation", so a vague
    task is a real failure a presence check would never catch.

    `researcher` (subagents/researcher/AGENT.md) is the fixture, as `onboarding-brief` was above: no
    `domains:` restriction, and its tools (`search_docs`, `calculator`, `query_employees`) match this file's
    fixtures. The question leans into the subagent's stated purpose ("a self-contained lookup whose reasoning
    doesn't need to appear in the main thread"), because SYSTEM_PROMPT says the model "may" delegate, never
    must, and an ambiguous prompt would test its optional judgment, not this mechanism.
    """
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = (
        "Look up Ecorp's support hours for me — I don't need to see how "
        "you found it, just the answer."
    )
    answer, tools_called = await _run_and_get_tool_calls(deepeval_ollama, question)

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        tools_called=tools_called,
        expected_tools=[ToolCall(name="run_subagent")],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            ToolCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
            ArgumentCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )


@pytest.fixture
def _use_crawl4ai_server(monkeypatch, crawl4ai_server):
    """NOT autouse: only `test_fetch_external_reference_tool_call_is_correct_and_well_argued` below needs it.
    `crawl4ai_server` (tests/deepeval/conftest.py, `tests/containers.py::ensure_crawl4ai()`) starts its own
    ephemeral container, and this fixture points `app.ingestion.web_crawler`'s
    `CRAWL4AI_SERVER_URL`/`CRAWL4AI_API_TOKEN` module globals at it (a monkeypatch, not an env var; see that
    helper's docstring). Same shape as tests/integration/test_web_crawler_live.py's fixture (2026-09-17; this
    used to be a plain reachability probe against an already-running `docker compose up -d crawl4ai`, skipped
    if not up).

    Deliberately NOT `@pytest.mark.crawl` on the test below, unlike every other real-crawl4ai test here,
    because of a real, disclosed CI ordering bug. This file's module-level `pytestmark = pytest.mark.deepeval`
    already applies to every test, so adding `crawl` made this ONE test match both `-m deepeval` (its home)
    AND test-live's `-m "llm or e2e or crawl"`. test-live never installs `deepeval`, so `deepeval_judge`
    fixture setup failed there with a hard `ModuleNotFoundError`, not a skip (caught in a real CI run).
    `.github/workflows/ci.yml`'s `deepeval` job used to run its own crawl4ai `docker run` sidecar for this;
    `ensure_crawl4ai()`'s self-provisioning replaced it (2026-09-17), so the test still gets a REAL run in CI
    without the `crawl` mark and without a sidecar step.
    """
    monkeypatch.setattr(web_crawler, "CRAWL4AI_SERVER_URL", crawl4ai_server["crawl4ai_server_url"])
    monkeypatch.setattr(web_crawler, "CRAWL4AI_API_TOKEN", crawl4ai_server["crawl4ai_api_token"])


async def test_fetch_external_reference_tool_call_is_correct_and_well_argued(
    deepeval_ollama, deepeval_judge, _use_crawl4ai_server
):
    """The fourth tool-mechanism gap this file closes: a REAL crawl4ai
    round trip through a REAL model's tool-selection judgment, not the
    fake-scripted `GenericFakeChatModel` `test_domain_crawl_tools_live.py`
    already uses to prove the crawl MECHANICS work. That file answers "does
    approved crawling actually render a page"; this one answers "does the
    model correctly recognize a customer-linked URL as a job for
    fetch_external_reference and call it with the right URL" — a genuine
    tool-selection/argument-quality question neither that file nor
    anything else in this suite asks with a real model.

    Needs `manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN` — this
    tool doesn't exist on the default Ecorp domain every OTHER test in this
    file uses (confirmed directly against app/domains/support/tools.py's
    own TOOLS list) — and `auto_approve=True`, since fetch_external_reference
    is an "outward" capability tool and always pauses for human_approval
    first, same as every other outward tool in this app."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    from app.domains.support.domain import SUPPORT_DOMAIN_PLUGIN, SUPPORT_MANIFEST

    question = "A customer linked this page and asked if it explains their issue: https://example.com"
    answer, tools_called = await _run_and_get_tool_calls(
        deepeval_ollama,
            question, manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN, auto_approve=True
        )

    judge = deepeval_judge
    test_case = LLMTestCase(
        input=question,
        actual_output=answer,
        tools_called=tools_called,
        expected_tools=[ToolCall(name="fetch_external_reference")],
        flaky=True,
    )
    assert_test(
        test_case,
        [
            ToolCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
            ArgumentCorrectnessMetric(model=judge, threshold=0.5, include_reason=True),
        ],
    )
