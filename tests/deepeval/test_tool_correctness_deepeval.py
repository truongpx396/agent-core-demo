"""Tool-call TRAJECTORY correctness against the real graph, via deepeval —
`@pytest.mark.deepeval`, `make deepeval`. See GRAPH_PATTERNS.md pattern 48
for the full writeup.

A genuinely different question from every other check in this suite, not a
smaller copy of one: `scripts/eval.py`'s `GoldenCase.expect_tool` checks
"was this tool called at all" — a binary presence check, never the
arguments it was called WITH. `test_rag_quality_deepeval.py` and
`test_conversation_simulator_deepeval.py` judge the FINAL ANSWER's
semantic quality, never the tool-call trajectory that produced it. Nothing
else here asks "did the agent call the right tool, and did it call it
well" as its own question.

Two metrics, two different reliability profiles — checked directly in
deepeval's own installed source, not assumed from the class names:
- `ToolCorrectnessMetric`'s core score (`_calculate_score()` ->
  `_calculate_non_exact_match_score()`) is a DETERMINISTIC name/type match
  between `tools_called` and `expected_tools` — no LLM call in that path
  at all. `model=`/`include_reason=True` only generates the natural-
  language explanation, never the verdict itself. Same reliability
  character as `scripts/eval.py`'s own `expect_tool` check, just richer
  (a real tool-call SET comparison, not just "was X called anywhere").
- `ArgumentCorrectnessMetric`'s core score genuinely IS LLM-judged
  (`measure()` -> `self._generate_verdicts(...)`, the same per-claim
  verdict pattern `FaithfulnessMetric` uses) — does the search QUERY this
  agent actually generated make sense for the question asked, not just
  "was search_docs called." This is the metric that needed the Groq judge
  fix (tests/deepeval/conftest.py's `deepeval_judge`) to be worth trusting at
  all; on the small local judge this suite used before that fix, a
  judgment call like "is this query well-formed" is exactly the kind of
  question that produced self-contradictory scores elsewhere in this
  pattern's own disclosed findings.

Six cases now, not three — two groups:
- `search_docs`/`calculator`/`query_employees`: one per tool this domain's
  small-model routing has actually been found confused about (see the
  SEEDING/collision finding below). `search_docs` reuses
  `test_rag_quality_deepeval.py`'s own "Ecorp support hours" wording
  deliberately — the same wording `scripts/eval.py`'s own
  `retrieval_company_topic_filter` golden case used to use, before that
  case (and `calculator_basic`) were retired 2026-09-16 in favor of this
  file's own richer tool-call/argument checks (see scripts/eval.py's own
  comment at the retirement site). `calculator` and `query_employees` are
  regression checks from the OTHER two directions —
  did tightening the prompt to fix the search_docs collision accidentally
  scope calculator or query_employees too narrowly to fire on a genuine
  question anymore? A prompt fix aimed at one failure mode overcorrecting
  into a new one is exactly what happened once already during this file's
  own fix (calculator briefly stopped firing on real math after an early
  SYSTEM_PROMPT edit — see app/agent/graph.py's own history).
- `skill_search`/`use_skill`, `run_subagent`, `fetch_external_reference`:
  added 2026-09-16 to close a real, disclosed gap — skills and subagents
  had ZERO live/semantic coverage anywhere in this repo before this (only
  hermetic, fake-LLM tests in tests/agent/), and crawl4ai's only live
  coverage (`test_domain_crawl_tools_live.py`) uses a scripted fake LLM
  too, proving the crawl MECHANICS work but never whether a REAL model
  correctly chooses to reach for it. Same two metrics, same judge, three
  more genuinely different questions this file didn't ask before.

SEEDING (2026-09-16) — this file is what surfaced the finding, not just a
consumer of it: an early, unseeded run of this exact test reliably called
`query_employees` instead of `search_docs` (a word collision between
"support hours" and the `Department.support` enum value), then — once
that collision was fixed at the prompt level — an unseeded re-verification
STILL failed, identically, across three different prompt edits. The
prompt edits turned out to be inert the whole time: `build_graph().ainvoke()`
never triggers `app/agent/runtime.py::_ensure_seeded_async`, the seeding
every production path relies on, so the agent was reasoning with ZERO
tool-routing guidance — not "the fix doesn't work," but "the fix was never
being tested." This file (and every sibling `build_graph().ainvoke()`
caller in this repo, `scripts/eval.py` included — see
tests/seeding.py's own `seed_thread` docstring for the full list)
now seeds properly before asserting anything. `scripts/eval.py`'s own
`retrieval_company_topic_filter` case was never actually a "proven"
baseline the way earlier comments here assumed — it has the identical gap
and needs its own re-verification, not just this file's.
"""
import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import graph as graph_module
from app.agent.graph import GraphDeps
from app.agent.graph_build import build_graph
from app.ingestion import web_crawler
from tests.conftest import TEST_CTX
from tests.seeding import seed_thread

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


def _real_search(query: str, ctx=None) -> tuple[str, list[dict]]:
    return _CONTEXT, _CITATIONS


@pytest.fixture(autouse=True)
def real_ollama_chat_model(monkeypatch, deepeval_ollama):
    monkeypatch.setattr(graph_module, "CHAT_MODEL", deepeval_ollama["model"])
    monkeypatch.setattr(graph_module, "OPENAI_API_BASE", deepeval_ollama["openai_api_base"])


async def _run_and_get_tool_calls(question: str, manifest=None, domain=None, auto_approve: bool = False):
    """Shared by every test below: build a fresh graph/thread, seed it
    properly (see tests/seeding.py's own `seed_thread` docstring —
    this is exactly the test file that surfaced why that matters), run one
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

    graph = build_graph(GraphDeps(search_docs=_real_search), manifest=manifest, domain=domain)
    config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}}
    await seed_thread(graph, config["configurable"]["thread_id"])
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


def test_search_docs_tool_call_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """The original case this file was built around — see its own module
    docstring for the real, disclosed word-collision finding this
    surfaced (fixed in app/agent/graph.py's SYSTEM_PROMPT and
    app/agent/tools.py's docstrings) and the seeding-bug finding this same
    test then went on to surface after that first fix (fixed across 9
    files — see tests/seeding.py)."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = "What are Ecorp's support hours?"
    answer, tools_called = asyncio.run(_run_and_get_tool_calls(question))

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


def test_calculator_tool_call_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
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
    app/agent/graph.py's own history/GRAPH_PATTERNS.md pattern 48)."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = "What is 12 times 7?"
    answer, tools_called = asyncio.run(_run_and_get_tool_calls(question))

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


def test_query_employees_tool_call_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
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
    answer, tools_called = asyncio.run(_run_and_get_tool_calls(question))

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


def test_skill_tool_calls_are_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """A genuinely different mechanism from the three tool-selection cases
    above: not "which of several DATA tools fits this question," but "does
    the model recognize a packaged, multi-step INSTRUCTION set exists for
    this task and route to it first," exactly as SYSTEM_PROMPT instructs
    ("For a task that might have packaged, multi-step instructions... call
    skill_search first; if it returns a good match, call use_skill with
    that exact name"). Zero live/semantic coverage of this existed before
    today — tests/agent/test_skills.py is hermetic (fake LLM), and nothing
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
    answer, tools_called = asyncio.run(_run_and_get_tool_calls(question))

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


def test_subagent_delegation_is_correct_and_well_argued(deepeval_ollama, deepeval_judge):
    """The other genuinely different mechanism nothing live/semantic
    tested before today: does the model correctly delegate a
    self-contained lookup to `run_subagent` when a bundled subagent's own
    description clearly fits, and — since ArgumentCorrectnessMetric is in
    play here too — does it hand that subagent a CLEAR, COMPLETE task
    description, which matters more here than anywhere else in this file:
    SYSTEM_PROMPT itself warns a subagent "has no access to this
    conversation," so a vague task description is a real failure mode a
    deterministic presence check would never catch.

    `researcher` (subagents/researcher/AGENT.md) is the right fixture for
    the same reason `onboarding-brief` was above: no `domains:` frontmatter
    restriction, and its own tool list (`search_docs`, `calculator`,
    `query_employees`) matches this file's existing fixtures exactly. The
    question is phrased to lean into the subagent's own stated purpose
    ("a self-contained lookup whose reasoning doesn't need to appear in the
    main thread") rather than leaving delegation ambiguous — SYSTEM_PROMPT
    only ever says the model "may" delegate, never must, so a genuinely
    ambiguous prompt would be testing the model's optional judgment call,
    not this mechanism's own correctness."""
    from deepeval import assert_test
    from deepeval.metrics import ArgumentCorrectnessMetric, ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    question = (
        "Look up Ecorp's support hours for me — I don't need to see how "
        "you found it, just the answer."
    )
    answer, tools_called = asyncio.run(_run_and_get_tool_calls(question))

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
    """NOT autouse — only `test_fetch_external_reference_tool_call_is_correct_and_well_argued`
    below needs it, unlike every other test in this file. `crawl4ai_server`
    (tests/deepeval/conftest.py, `tests/containers.py::ensure_crawl4ai()`)
    starts its own ephemeral container and this fixture points
    `app.ingestion.web_crawler`'s own `CRAWL4AI_SERVER_URL`/
    `CRAWL4AI_API_TOKEN` module globals at it (see that helper's own
    docstring for why a monkeypatch, not an env var) — same shape
    tests/integration/test_web_crawler_live.py's own fixture uses (2026-09-17;
    previously a plain reachability probe against an already-running
    `docker compose up -d crawl4ai`, skipped cleanly if not up).

    Deliberately NOT `@pytest.mark.crawl` on the test below, unlike every
    other real-crawl4ai test in this repo — a real, disclosed CI ordering
    bug, not an oversight: this file's module-level `pytestmark =
    pytest.mark.deepeval` already applies to every test here, so adding
    `crawl` on top made this ONE test match BOTH `-m deepeval` (this file's
    real home) AND test-live's own `-m "llm or e2e or crawl"` selection —
    and test-live's job never installs the `deepeval` package, so
    `deepeval_judge` fixture setup failed there with a hard
    `ModuleNotFoundError`, not a graceful skip (caught directly in a real
    CI run of this exact combination). `.github/workflows/ci.yml`'s
    `deepeval` job used to run its own crawl4ai `docker run` sidecar for
    exactly this reason; `ensure_crawl4ai()`'s self-provisioning replaced
    it (2026-09-17), so this test still gets a REAL run in CI despite
    dropping the `crawl` mark, with no separate sidecar step needed."""
    monkeypatch.setattr(web_crawler, "CRAWL4AI_SERVER_URL", crawl4ai_server["crawl4ai_server_url"])
    monkeypatch.setattr(web_crawler, "CRAWL4AI_API_TOKEN", crawl4ai_server["crawl4ai_api_token"])


def test_fetch_external_reference_tool_call_is_correct_and_well_argued(
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
    answer, tools_called = asyncio.run(
        _run_and_get_tool_calls(
            question, manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN, auto_approve=True
        )
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
